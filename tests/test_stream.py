"""
Offline tests for data/schwab_stream.py — the real-time StreamClient layer.

No socket, no network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_stream.py
    python tests/test_stream.py

The websocket/loop itself is live-only (covered by the smoke test in the plan);
here we test the PURE pieces it's built from: the per-contract premium aggregation
(StreamedContract → OHLC + interval volume + roll), the /ES field mapper feeding
ESTracker, the ATM-symbol extraction, the subscription diff, the freshness gate,
and a streamed wick candle flowing through MocEngine.feed_stream into capitulation.
"""

from __future__ import annotations

import asyncio
import time

from helenus.config import MocConfig
from helenus.data.schwab_stream import (
    SchwabStream,
    StreamedContract,
    _diff_symbols,
    _es_stream_to_quote,
    atm_option_symbols,
)
from helenus.engine.intermarket import ESTracker
from helenus.engine.moc import MocEngine
from helenus.engine.scan2 import Bar, Direction, MarketState

import datetime as dt
from helenus.data.schwab_feed import ET


# --------------------------------------------------------------------------- #
# StreamedContract — premium OHLC + interval volume
# --------------------------------------------------------------------------- #

def test_streamed_contract_builds_ohlc_and_volume() -> None:
    c = StreamedContract("SPXW  260623C07450000")
    c.observe(mark=2.0, cum_volume=100.0)   # baseline volume → no delta yet
    c.observe(mark=3.0, cum_volume=150.0)   # +50
    c.observe(mark=1.5, cum_volume=170.0)   # +20
    candle, marks, iv = c.roll()
    assert candle == (2.0, 3.0, 1.5, 1.5)   # open, high (the wick), low, close
    assert marks == [2.0, 3.0, 1.5]
    assert iv == 70.0


def test_streamed_contract_roll_resets_but_keeps_cum_baseline() -> None:
    c = StreamedContract("X")
    c.observe(mark=2.0, cum_volume=100.0)
    c.observe(mark=2.5, cum_volume=140.0)
    c.roll()
    # Next bar: a fresh candle, but the cumulative-volume baseline persists so the
    # delta is correct across the bar boundary.
    empty_candle, empty_marks, empty_iv = c.roll()
    assert empty_candle is None and empty_marks == [] and empty_iv == 0.0
    c.observe(mark=3.0, cum_volume=160.0)   # +20 vs the carried 140 baseline
    _candle, _marks, iv = c.roll()
    assert iv == 20.0


# --------------------------------------------------------------------------- #
# /ES field mapper → ESTracker
# --------------------------------------------------------------------------- #

def test_es_stream_maps_into_estracker() -> None:
    raw = {
        "LAST_PRICE": 5300.0,
        "BID_SIZE": 40.0,
        "ASK_SIZE": 10.0,
        "TOTAL_VOLUME": 1_000_000.0,
        "FUTURE_SETTLEMENT_PRICE": 5280.0,
    }
    q = _es_stream_to_quote(raw)
    assert q["lastPrice"] == 5300.0
    assert round(q["netPercentChange"], 3) == round((5300 / 5280 - 1) * 100, 3)
    r = ESTracker().update(q)
    assert r.bid_size == 40.0 and r.ask_size == 10.0
    assert round(r.imbalance, 2) == 0.6        # (40-10)/50
    assert r.last == 5300.0


def test_es_stream_prefers_explicit_percent() -> None:
    q = _es_stream_to_quote({"LAST_PRICE": 5300.0, "FUTURE_CHANGE_PERCENT": 0.5})
    assert q["netPercentChange"] == 0.5


def test_es_stream_omits_missing_fields() -> None:
    # A delta tick carrying only sizes → no lastPrice key, so ESTracker's _num
    # falls through to 0.0 rather than being poisoned with NaN.
    q = _es_stream_to_quote({"BID_SIZE": 5.0, "ASK_SIZE": 5.0})
    assert "lastPrice" not in q and q["bidSize"] == 5.0
    assert ESTracker().update(q).last == 0.0


# --------------------------------------------------------------------------- #
# Subscription diff + ATM symbol extraction
# --------------------------------------------------------------------------- #

def test_diff_symbols() -> None:
    add, remove = _diff_symbols({"A", "B"}, {"B", "C"})
    assert add == {"C"} and remove == {"A"}


def test_atm_option_symbols_picks_nearest_strike() -> None:
    payload = {
        "callExpDateMap": {"2026-06-23:0": {
            "7450.0": [{"strikePrice": 7450, "symbol": "C7450"}],
            "7460.0": [{"strikePrice": 7460, "symbol": "C7460"}],
        }},
        "putExpDateMap": {"2026-06-23:0": {
            "7440.0": [{"strikePrice": 7440, "symbol": "P7440"}],
            "7430.0": [{"strikePrice": 7430, "symbol": "P7430"}],
        }},
    }
    call, put = atm_option_symbols(payload, 7451.0)
    assert call == "C7450" and put == "P7440"


# --------------------------------------------------------------------------- #
# Freshness gate + accessors (no socket)
# --------------------------------------------------------------------------- #

def test_freshness_gate() -> None:
    s = SchwabStream(feed=None)
    assert not s.is_fresh("es")               # not connected → never fresh
    s._connected = True
    s._last["es"] = time.monotonic()
    assert s.is_fresh("es")
    s._last["es"] = time.monotonic() - 10_000
    assert not s.is_fresh("es")               # stale


def test_es_quote_gated_on_freshness() -> None:
    s = SchwabStream(feed=None)
    s._connected = True
    s._es_raw = {"LAST_PRICE": 5300.0, "BID_SIZE": 40.0, "ASK_SIZE": 10.0}
    s._last["es"] = time.monotonic()
    assert s.es_quote()["bidSize"] == 40.0
    s._last["es"] = 0.0
    assert s.es_quote() == {}                 # stale → empty → bot falls back to REST


def test_set_option_symbols_records_roles_when_offline() -> None:
    s = SchwabStream(feed=None)               # not connected → no sub calls, just roles
    asyncio.run(s.set_option_symbols("C1", "P1"))
    assert s._roles == {"call": "C1", "put": "P1"}


# --------------------------------------------------------------------------- #
# Streamed wick candle → MOC capitulation (the headline win)
# --------------------------------------------------------------------------- #

def _payload(o, h, l, c, iv=0.0):
    return ((o, h, l, c), [o, h, c], iv)


def test_streamed_wick_drives_moc_capitulation() -> None:
    cfg = MocConfig(ema_fast=2, ema_slow=3, ema_trend=4)
    eng = MocEngine(cfg)
    st = MarketState()
    st.push_bar(Bar(ts=dt.datetime(2026, 6, 23, 11, 0, tzinfo=ET),
                    open=7400, high=7400, low=7400, close=7400, volume=100.0))
    # Warm the call-premium EMAs low (~1.0) over several bars via the stream path.
    for m in range(6):
        eng.feed_stream(_payload(1.0, 1.0, 1.0, 1.0), None)
        eng.on_bar(st, None, dt.datetime(2026, 6, 23, 11, m, tzinfo=ET))
    # A streamed call candle that WICKS to 8.5 then closes back at 1.2 (rejection).
    eng.feed_stream(_payload(1.0, 8.5, 1.0, 1.2), None)
    r = eng.on_bar(st, None, dt.datetime(2026, 6, 23, 11, 10, tzinfo=ET))
    assert r.capitulation and r.cap_side == "call"
    assert r.cap_high == 8.5 and r.cap_close == 1.2


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
