"""
Offline tests for engine/moc.py — the Market-On-Close (power-hour close) engine.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_moc.py
    python tests/test_moc.py

Asserts the mechanics: the window phases (pre → brief → reversal[3:50–3:55] → late →
closed); the inverse 5m candle-color heuristic (green→bearish, red→bullish); the
GEX pin/overshoot context; the simplified premium-behavior REVERSAL (a side bases +
its volume surges vs the other → reversal that way, but only inside the 3:50–3:55
window); the CAPITULATION candle (a premium wick above its EMAs, fires intraday);
and the daily reset.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from helenus.config import MocConfig
from helenus.data.schwab_feed import ET
from helenus.engine.moc import MocEngine
from helenus.engine.scan2 import Bar, Direction, MarketState

_DAY = dt.date(2026, 6, 23)


def _ts(hour, minute, day=_DAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def _chain(spot, call_prem, put_prem, call_vol, put_vol, strike=None):
    """A minimal 0DTE chain payload with one ATM call + one ATM put."""
    strike = strike if strike is not None else round(spot)

    def contract(side, prem, vol):
        return {
            "strikePrice": strike,
            "delta": 0.5 if side == "call" else -0.5,
            "gamma": 0.01,
            "mark": prem,
            "bid": prem * 0.98,
            "ask": prem * 1.02,
            "totalVolume": vol,
            "symbol": f"SPXW  260623{'C' if side == 'call' else 'P'}{strike}",
        }

    key = f"{strike}"
    return {
        "callExpDateMap": {"2026-06-23:0": {key: [contract("call", call_prem, call_vol)]}},
        "putExpDateMap": {"2026-06-23:0": {key: [contract("put", put_prem, put_vol)]}},
    }


def _state_with_5m(closes, opens=None, day=_DAY):
    """A MarketState whose last 5 one-min bars close at 15:46…15:50 (the candle
    feeding the heuristic into 3:50)."""
    st = MarketState()
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        ts = _ts(15, 46 + i, day)
        st.push_bar(Bar(ts=ts, open=float(o), high=float(max(o, c)),
                        low=float(min(o, c)), close=float(c), volume=100.0))
    return st


# --------------------------------------------------------------------------- #
# Window phases
# --------------------------------------------------------------------------- #

def test_window_phases() -> None:
    eng = MocEngine()
    st = _state_with_5m([7400, 7400, 7400, 7400, 7400])
    assert eng.on_bar(st, None, _ts(15, 30)).phase == "pre"
    assert eng.on_bar(st, None, _ts(15, 48)).phase == "brief"
    rev = eng.on_bar(st, None, _ts(15, 52))
    assert rev.phase == "reversal" and rev.in_reversal_window and rev.in_window
    late = eng.on_bar(st, None, _ts(15, 57))
    assert late.phase == "late" and late.in_window and not late.in_reversal_window
    assert eng.on_bar(st, None, _ts(16, 1)).phase == "closed"


# --------------------------------------------------------------------------- #
# 5m candle-color heuristic (INVERSE: green→bearish, red→bullish)
# --------------------------------------------------------------------------- #

def test_heuristic_green_is_bearish() -> None:
    eng = MocEngine()
    st = _state_with_5m([7401, 7402, 7403, 7404, 7405], opens=[7400] * 5)
    r = eng.on_bar(st, None, _ts(15, 52))
    assert r.heuristic_color == "green" and r.heuristic_bias is Direction.BEARISH


def test_heuristic_red_is_bullish() -> None:
    eng = MocEngine()
    st = _state_with_5m([7399, 7398, 7397, 7396, 7395], opens=[7400] * 5)
    r = eng.on_bar(st, None, _ts(15, 52))
    assert r.heuristic_color == "red" and r.heuristic_bias is Direction.BULLISH


# --------------------------------------------------------------------------- #
# GEX pin / overshoot context
# --------------------------------------------------------------------------- #

def _gex(spot, call_walls, put_walls, zero_gamma=None):
    return SimpleNamespace(
        spot=spot, call_walls=call_walls, put_walls=put_walls, zero_gamma=zero_gamma
    )


def test_gex_state_pin_and_overshoot() -> None:
    eng = MocEngine()
    st = _state_with_5m([7400] * 5)
    pin = _gex(7450.0, [(7452.0, 1.0)], [(7440.0, -1.0)], 7445.0)   # 2pt from a wall
    assert eng.on_bar(st, pin, _ts(15, 52)).gex_state == "PIN"
    over = _gex(7460.0, [(7452.0, 1.0)], [(7440.0, -1.0)], 7445.0)  # above top call wall
    assert eng.on_bar(st, over, _ts(15, 52)).gex_state == "OVERSHOOT"
    neutral = _gex(7448.0, [(7460.0, 1.0)], [(7430.0, -1.0)], 7400.0)
    assert eng.on_bar(st, neutral, _ts(15, 52)).gex_state == "NEUTRAL"


# --------------------------------------------------------------------------- #
# The premium-behavior reversal (priority play)
# --------------------------------------------------------------------------- #

def _feed_basing_put(eng, spot=7400.0):
    """Put premium declines then bases (higher-low) while put volume surges vs
    calls — the 7375P tell. Calls stay flat with trivial volume."""
    put_prems = [10, 8, 6, 4, 3, 4, 5, 6, 7, 8]
    put_cum = 1000.0
    call_cum = 500.0
    for i, pp in enumerate(put_prems):
        put_cum += 600.0      # heavy fresh put volume each poll
        call_cum += 10.0      # trivial call volume
        eng.on_chain(_chain(spot, call_prem=5.0, put_prem=float(pp),
                            call_vol=call_cum, put_vol=put_cum), spot)


def test_reversal_put_side_is_bearish_in_window() -> None:
    eng = MocEngine()
    st = _state_with_5m([7400] * 5)
    eng.on_bar(st, None, _ts(15, 51))     # establish the date / reset
    _feed_basing_put(eng)
    r = eng.on_bar(st, None, _ts(15, 52))  # inside 3:50–3:55
    assert r.basing_side == "put"
    assert r.reversal_direction is Direction.BEARISH
    assert r.reversal_active and r.volume_surge_ratio > 1.5


def test_reversal_inactive_outside_window() -> None:
    eng = MocEngine()
    st = _state_with_5m([7400] * 5)
    eng.on_bar(st, None, _ts(11, 0))
    _feed_basing_put(eng)
    r = eng.on_bar(st, None, _ts(11, 1))   # midday — the basing read exists…
    assert r.basing_side == "put"
    assert not r.reversal_active           # …but the reversal only fires in-window


def test_reversal_call_side_is_bullish() -> None:
    eng = MocEngine()
    st = _state_with_5m([7400] * 5)
    eng.on_bar(st, None, _ts(15, 51))
    call_prems = [10, 8, 6, 4, 3, 4, 5, 6, 7, 8]
    put_cum, call_cum = 500.0, 1000.0
    for cp in call_prems:
        call_cum += 600.0
        put_cum += 10.0
        eng.on_chain(_chain(7400.0, call_prem=float(cp), put_prem=5.0,
                            call_vol=call_cum, put_vol=put_cum), 7400.0)
    r = eng.on_bar(st, None, _ts(15, 52))
    assert r.basing_side == "call" and r.reversal_direction is Direction.BULLISH and r.reversal_active


# --------------------------------------------------------------------------- #
# The capitulation candle (fires intraday)
# --------------------------------------------------------------------------- #

def test_capitulation_wick_above_emas() -> None:
    # Small EMA periods so they mature quickly in a unit test.
    cfg = MocConfig(ema_fast=2, ema_slow=3, ema_trend=4)
    eng = MocEngine(cfg)
    spot = 7400.0
    # Warm the call-premium EMAs low (~1.0) over several 1m candles.
    for m in range(6):
        eng.on_chain(_chain(spot, call_prem=1.0, put_prem=1.0, call_vol=100, put_vol=100), spot)
        eng.on_bar(_state_with_5m([7400] * 5), None, _ts(11, m))
    # A candle whose call premium WICKS to 8.5 then closes back at 1.2 (rejection).
    eng.on_chain(_chain(spot, call_prem=1.0, put_prem=1.0, call_vol=200, put_vol=200), spot)
    eng.on_chain(_chain(spot, call_prem=8.5, put_prem=1.0, call_vol=300, put_vol=200), spot)
    eng.on_chain(_chain(spot, call_prem=1.2, put_prem=1.0, call_vol=400, put_vol=200), spot)
    r = eng.on_bar(_state_with_5m([7400] * 5), None, _ts(11, 10))
    assert r.capitulation and r.cap_side == "call"
    assert r.cap_high == 8.5 and r.cap_close == 1.2


# --------------------------------------------------------------------------- #
# Daily reset
# --------------------------------------------------------------------------- #

def test_resets_on_a_new_day() -> None:
    eng = MocEngine()
    st = _state_with_5m([7400] * 5)
    eng.on_bar(st, None, _ts(15, 51))
    _feed_basing_put(eng)
    assert len(eng.put.prem) > 0
    day2 = dt.date(2026, 6, 24)
    eng.on_bar(_state_with_5m([7400] * 5, day=day2), None, _ts(15, 51, day2))
    assert len(eng.put.prem) == 0 and eng.put.last_candle is None


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
