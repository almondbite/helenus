"""
Offline tests for the Task-2 scalp mode — 5-minute bars + ITM contracts,
promoted to a PRIMARY confluence (ScalpConfig.confluence_5m, the default).

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_scalp5m.py
    python tests/test_scalp5m.py

Contract under test: ALL ignition/cross/bar-close logic runs on 5m bars folded
from the 1m tape (a bar whose close-stamp minute is divisible by 5 closes its
bucket immediately — zero lag beyond the 5m boundary; a tape gap flushes the
stale bucket on the next push), the contract leg is ITM-only (calls at/below
spot, puts at/above, |delta| ≈ itm_target_delta), the chop window scales ×5,
the trend EMA runs at 40 periods (≈ the wall-clock 200×1m line), and the
EMA-ignition candidate outranks the flow/vanna triggers in detect_candidate.
The 5m read confirms later than 1m by design — reliability over earliness.
The legacy 1m/OTM semantics stay covered by tests/test_scalp.py.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from contextlib import contextmanager

import pandas as pd

from helenus.config import CONFIG
from helenus.data.schwab_feed import ET
from helenus.engine.analyst import _assemble_system_prompt, _scalp_block
from helenus.engine.flow import VannaReading
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Bar, Direction, MarketState, TriggerType, detect_candidate
from helenus.engine.scalp import (
    ContractQuote,
    ScalpEngine,
    extract_contracts,
    select_target_contract,
)

LEGACY = dataclasses.replace(CONFIG.scalp, confluence_5m=False)


@contextmanager
def scalp_mode(confluence_5m: bool):
    """Pin the scalp mode for module-level readers (detect_candidate precedence,
    _scalp_block). Engines capture their cfg at construction, so most tests just
    pass a cfg instead."""
    old = CONFIG.scalp
    object.__setattr__(
        CONFIG, "scalp", dataclasses.replace(old, confluence_5m=confluence_5m)
    )
    try:
        yield
    finally:
        object.__setattr__(CONFIG, "scalp", old)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

_T0 = dt.datetime(2026, 6, 22, 13, 0, tzinfo=ET)

# Same shape as test_scalp's fixture: decline then a rising run, the 5/9 cross
# landing on the FINAL value — here each value is one 5m close.
_CROSS_UP_5M = list(range(5012, 4994, -1)) + list(range(4996, 5002))


def _bar(minute_offset: int, c: float, *, o=None, h=None, l=None, v=50.0) -> Bar:
    """A 1m tape bar stamped `minute_offset` minutes after 13:00 (close-stamped,
    like the bot's bar assembly)."""
    return Bar(
        ts=_T0 + dt.timedelta(minutes=minute_offset),
        open=float(c if o is None else o),
        high=float(c if h is None else h),
        low=float(c if l is None else l),
        close=float(c),
        volume=v,
    )


def _bars_5m_series(closes, *, heavy_tail_5m: int = 0) -> list[Bar]:
    """Each value in `closes` becomes FIVE identical 1m bars stamped :x1…:x5, so
    the folded 5m close series equals `closes` exactly. `heavy_tail_5m` marks the
    last N 5m buckets with heavy volume (pulls session VWAP below the final
    close, mirroring test_scalp's heavy_tail)."""
    out: list[Bar] = []
    n = len(closes)
    for i, c in enumerate(closes):
        heavy = heavy_tail_5m and i >= n - heavy_tail_5m
        for j in range(5):
            out.append(_bar(i * 5 + j + 1, c, v=8000.0 if heavy else 50.0))
    return out


def _green_gex(spot: float = 5001.0) -> GexProfile:
    return GexProfile(
        spot=spot, total_net_gex=-5e8, zero_gamma=5050.0,
        call_walls=[(5030.0, -1.0)], put_walls=[(4970.0, 1.0)],
        by_strike=pd.DataFrame(),
    )


def _vanna(vix_change: float = 0.0) -> VannaReading:
    return VannaReading(
        vix_change=vix_change, vix_falling=vix_change < 0, otm_call_flow=0.0,
        otm_put_flow=0.0, call_flow_dominance=0.0, active=False,
        bearish_active=False, label="x", note="x",
    )


def _itm_chain(spot: float = 5001.0) -> dict:
    """Chain carrying BOTH the legacy OTM picks and the new ITM strikes, so the
    two selection modes are distinguishable."""
    def c(strike, delta, mark=3.0):
        return {"strikePrice": strike, "mark": mark, "bid": mark - 0.1,
                "ask": mark + 0.1, "delta": delta, "gamma": 0.02, "totalVolume": 500}
    return {
        "underlying": {"mark": spot},
        "callExpDateMap": {"k": {
            "4995.0": [c(4995, 0.65, mark=9.0)],     # ITM (below spot)
            "5010.0": [c(5010, 0.35)],               # legacy OTM pick
        }},
        "putExpDateMap": {"k": {
            "5005.0": [c(5005, -0.65, mark=9.0)],    # ITM (above spot)
            "4990.0": [c(4990, -0.35)],              # legacy OTM pick
        }},
    }


def _feed(eng: ScalpEngine, bars: list[Bar], gex=None, vanna=None):
    """Push bars through a MarketState + the engine; returns (state, readings)."""
    st = MarketState()
    readings = []
    for b in bars:
        st.push_bar(b)
        readings.append(eng.on_bar(st, gex, vanna, None))
    return st, readings


# --------------------------------------------------------------------------- #
# 5m folding
# --------------------------------------------------------------------------- #

def test_fold_aggregates_ohlcv_by_hand() -> None:
    # Bars stamped :31…:35, distinct OHLCV → one 5m bar: open of the first,
    # extremes across all five, close of the last, summed volume.
    eng = ScalpEngine()
    bars = [
        _bar(31, 100.0, o=99.0, h=101.0, l=98.5, v=10),
        _bar(32, 102.0, h=103.0, l=99.5, v=20),
        _bar(33, 101.0, h=102.5, l=100.0, v=30),
        _bar(34, 104.0, h=105.0, l=100.5, v=40),
        _bar(35, 103.0, h=104.5, l=102.0, v=50),
    ]
    _feed(eng, bars)
    assert len(eng._bars5) == 1
    b5 = eng._bars5[-1]
    assert b5.open == 99.0 and b5.high == 105.0 and b5.low == 98.5
    assert b5.close == 103.0 and b5.volume == 150.0
    assert b5.ts == bars[-1].ts


def test_emas_update_once_per_5m_close() -> None:
    # :31…:45 = exactly three 5m closes (:35, :40, :45) → the fast EMA has seen 3
    # samples, no more (a 1m engine would have seen 15).
    eng = ScalpEngine()
    _feed(eng, [_bar(31 + i, 100.0 + i) for i in range(15)])
    assert eng.ema.fast.n == 3


def test_trigger_only_on_the_5m_close_zero_lag() -> None:
    eng = ScalpEngine()
    _, readings = _feed(eng, _bars_5m_series(_CROSS_UP_5M), _green_gex(), _vanna())
    # Forming bars carry no trigger and say so; every direction-carrying reading
    # lands on a bucket-final (:x5/:x0-stamped) bar — the 5m close itself.
    for b_idx, r in enumerate(readings):
        minute = (b_idx + 1) % 5  # bars stamped :01… → final bars at idx%5==4
        if r.direction is not None:
            assert minute == 0, f"trigger on a forming bar (idx {b_idx})"
    # The final 5m close carries the bullish 5/9 cross (same shape as the 1m
    # fixture — the series just lives on 5m closes now).
    last = readings[-1]
    assert last.direction is Direction.BULLISH and last.cross_type == "cross"
    assert "5m 5/9 cross" in last.note


def test_forming_bars_surface_live_state() -> None:
    eng = ScalpEngine()
    _, readings = _feed(eng, [_bar(31, 100.0), _bar(32, 101.0)])
    assert all("5m bar forming" in r.note for r in readings)
    assert all(r.direction is None for r in readings)


def test_gap_flushes_the_stale_bucket() -> None:
    # :31…:34 accumulate, :35 never prints; the :37 bar (a NEW bucket) first
    # flushes the stale :30 bucket — its close is the :34 close.
    eng = ScalpEngine()
    bars = [_bar(31, 100.0), _bar(32, 101.0), _bar(33, 102.0), _bar(34, 103.0)]
    _feed(eng, bars)
    assert eng.ema.fast.n == 0 and len(eng._bars5) == 0
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    st.push_bar(_bar(37, 105.0))
    eng.on_bar(st, None, None, None)
    assert eng.ema.fast.n == 1
    assert eng._bars5[-1].close == 103.0


def test_warm_seeds_5m_emas_without_reprocessing() -> None:
    bars = _bars_5m_series(_CROSS_UP_5M)
    eng = ScalpEngine()
    eng.warm(bars)
    assert eng.ema.fast.n == len(_CROSS_UP_5M)   # one update per 5m close
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    r = eng.on_bar(st, _green_gex(), _vanna(), None)
    assert r.direction is None                    # already consumed by warm()


# --------------------------------------------------------------------------- #
# ITM contract leg
# --------------------------------------------------------------------------- #

def test_select_picks_itm_strikes() -> None:
    contracts = extract_contracts(_itm_chain())
    call = select_target_contract(contracts, 5001.0, Direction.BULLISH)
    put = select_target_contract(contracts, 5001.0, Direction.BEARISH)
    assert call is not None and call.strike == 4995 and call.strike <= 5001
    assert put is not None and put.strike == 5005 and put.strike >= 5001
    assert abs(call.delta) >= 0.5 and abs(put.delta) >= 0.5


def test_legacy_select_still_picks_otm() -> None:
    contracts = extract_contracts(_itm_chain())
    call = select_target_contract(contracts, 5001.0, Direction.BULLISH, LEGACY)
    put = select_target_contract(contracts, 5001.0, Direction.BEARISH, LEGACY)
    assert call is not None and call.strike == 5010
    assert put is not None and put.strike == 4990


def test_on_chain_tracks_nearest_itm_wings() -> None:
    # Spot 5002: the ATM-nearest strikes are the OTM 5003C/5001P, but the ITM
    # mandate tracks 5000C (mark 6.0) and 5004P (mark 7.0).
    def c(strike, delta, mark):
        return {"strikePrice": strike, "mark": mark, "bid": mark - 0.1,
                "ask": mark + 0.1, "delta": delta, "gamma": 0.02, "totalVolume": 100}
    chain = {
        "underlying": {"mark": 5002.0},
        "callExpDateMap": {"k": {
            "5000.0": [c(5000, 0.60, 6.0)], "5003.0": [c(5003, 0.45, 4.0)],
        }},
        "putExpDateMap": {"k": {
            "5004.0": [c(5004, -0.60, 7.0)], "5001.0": [c(5001, -0.45, 3.5)],
        }},
    }
    eng = ScalpEngine()
    eng.on_chain(chain, 5002.0)
    assert eng._atm_call[-1] == 6.0
    assert eng._atm_put[-1] == 7.0
    legacy = ScalpEngine(LEGACY)
    legacy.on_chain(chain, 5002.0)
    assert legacy._atm_call[-1] == 4.0
    assert legacy._atm_put[-1] == 3.5


def test_full_gate_stack_active_on_5m() -> None:
    # The ported test_active_when_all_gates_pass: heavy tail drags VWAP below the
    # final close, the ITM chain supplies the leg, room to Round 5010 is 9pt.
    eng = ScalpEngine()
    chain = _itm_chain()
    eng.on_chain(chain, 5001.0)
    _, readings = _feed(
        eng, _bars_5m_series(_CROSS_UP_5M, heavy_tail_5m=6), _green_gex(), _vanna()
    )
    r = readings[-1]
    assert r.direction is Direction.BULLISH and r.active
    assert r.target_contract is not None and r.target_contract.strike == 4995
    assert r.premium_target is not None


# --------------------------------------------------------------------------- #
# Chop window + trend EMA scaling
# --------------------------------------------------------------------------- #

def test_chop_window_scales_with_timeframe() -> None:
    eng = ScalpEngine()
    assert eng._chop_window == CONFIG.scalp.chop_window_min * CONFIG.scalp.bar_minutes
    assert ScalpEngine(LEGACY)._chop_window == CONFIG.scalp.chop_window_min
    # Crosses 24/20/10 minutes back all sit inside the scaled 25m window…
    now = _T0 + dt.timedelta(hours=2)
    for mins in (24, 20, 10):
        eng.chop.record(now - dt.timedelta(minutes=mins))
    assert eng.chop.count(now, eng._chop_window) == 3
    # …but a 26-minute-old one falls out.
    eng2 = ScalpEngine()
    eng2.chop.record(now - dt.timedelta(minutes=26))
    assert eng2.chop.count(now, eng2._chop_window) == 0


def test_trend_ema_is_40_periods_on_5m() -> None:
    assert ScalpEngine().ema.trend.period == CONFIG.scalp.ema_trend_5m == 40
    assert ScalpEngine(LEGACY).ema.trend.period == CONFIG.scalp.ema_trend == 200


# --------------------------------------------------------------------------- #
# Gate promotion — EMA ignition outranks the flow triggers
# --------------------------------------------------------------------------- #

def _vanna_stub():
    from types import SimpleNamespace
    return SimpleNamespace(
        active=True, bearish_active=False,
        inflection_active=False, inflection_bearish=False, note="vanna",
    )


def _scalp_stub():
    from types import SimpleNamespace
    return SimpleNamespace(active=True, target_level=None, note="ema ignition")


def test_promoted_scalp_outranks_vanna() -> None:
    st = MarketState()
    st.push_bar(_bar(1, 7445.0))
    st.push_bar(_bar(2, 7446.0))
    gex = GexProfile(
        spot=7446.0, total_net_gex=0.0, zero_gamma=7600.0,
        call_walls=[], put_walls=[], by_strike=pd.DataFrame(),
    )
    with scalp_mode(True):
        cand = detect_candidate(st, gex, vanna=_vanna_stub(), scalp=_scalp_stub())
    assert cand is not None and cand.trigger is TriggerType.EMA_IGNITION
    with scalp_mode(False):
        cand = detect_candidate(st, gex, vanna=_vanna_stub(), scalp=_scalp_stub())
    assert cand is not None and cand.trigger is TriggerType.VANNA_RALLY


# --------------------------------------------------------------------------- #
# Prompt + snapshot surfaces
# --------------------------------------------------------------------------- #

def test_prompt_5m_variant_states_the_tradeoff() -> None:
    p = _assemble_system_prompt(False, True)
    assert "5m 5/9 ITM contract scalp" in p
    assert "A PRIMARY CONFLUENCE" in p
    assert "TIMEFRAME TRADEOFF IS INTENTIONAL" in p
    assert "1m 5/9 contract scalp" not in p


def test_prompt_legacy_scalp_section_preserved() -> None:
    p = _assemble_system_prompt(True, False)   # full legacy: GEX + 1m scalp
    assert "1m 5/9 contract scalp" in p
    assert "THE GEX MAP IS YOUR PRIMARY FRAME" in p
    assert "TIMEFRAME TRADEOFF" not in p


def test_scalp_block_reports_mode() -> None:
    reading_kw = dict(direction=Direction.BULLISH, cross_type="cross")
    from helenus.engine.scalp import ScalpReading
    with scalp_mode(True):
        block = _scalp_block(ScalpReading(**reading_kw))
        assert block["timeframe"] == "5m" and block["contract_style"] == "ITM"
    with scalp_mode(False):
        block = _scalp_block(ScalpReading(**reading_kw))
        assert block["timeframe"] == "1m" and block["contract_style"] == "OTM"


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
