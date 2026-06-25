"""
Offline tests for engine/es_lead.py + the scan2 ES_LEAD two-stage gate.

No socket, no network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_es_lead.py
    python tests/test_es_lead.py

Covers the leading /ES thrust detector (the dual ATR + points gate on the /ES
bar-close series) and the two-stage candidate: ARM on /ES, CONFIRM on SPX.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from helenus.config import ESLeadConfig
from helenus.engine.es_lead import ESLeadReading, ESLeadTracker
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Bar, Direction, MarketState, TriggerType, detect_candidate


_CFG = ESLeadConfig(enabled=True, lookback_bars=3, atr_mult=2.0, min_pts=4.0,
                    arm_ttl_bars=2, confirm_min_pts=1.0)


def _feed(closes: list[float]) -> ESLeadReading:
    t = ESLeadTracker(_CFG)
    for c in closes:
        t.observe(c)
    return t.reading()


def _bar(c: float, h: float | None = None, l: float | None = None, v: float = 100.0) -> Bar:
    return Bar(
        ts=dt.datetime(2026, 6, 24, 11, 0),
        open=c, high=c if h is None else h, low=c if l is None else l, close=c, volume=v,
    )


def _gex(spot: float, zero_gamma: float | None = None) -> GexProfile:
    return GexProfile(
        spot=spot, total_net_gex=0.0, zero_gamma=zero_gamma,
        call_walls=[], put_walls=[], by_strike=pd.DataFrame(),
    )


# --------------------------------------------------------------------------- #
# ESLeadTracker — the /ES thrust gate
# --------------------------------------------------------------------------- #

def test_up_thrust_is_bullish() -> None:
    r = _feed([100.0, 100.5, 101.0, 106.0])   # disp +6, ATR 2.0 → 3.0× ATR
    assert r.break_direction is Direction.BULLISH
    assert r.displacement_pts == 6.0 and r.atr_mult == 3.0


def test_down_thrust_is_bearish() -> None:
    r = _feed([100.0, 99.5, 99.0, 94.0])      # disp -6
    assert r.break_direction is Direction.BEARISH


def test_flat_tape_no_break() -> None:
    assert _feed([100.0, 100.5, 100.0, 100.5]).break_direction is None


def test_clears_atr_but_below_points_floor() -> None:
    # disp +3 (≥ 2× a tiny ATR) but below the 4pt floor → no break.
    r = _feed([100.0, 100.1, 100.2, 103.0])
    assert abs(r.displacement_pts) < _CFG.min_pts
    assert r.break_direction is None


def test_clears_points_but_below_atr_mult() -> None:
    # disp +5 (≥ 4pt) but choppy big bars → |disp| < 2× ATR → no break.
    r = _feed([100.0, 110.0, 95.0, 105.0])
    assert r.displacement_pts == 5.0
    assert r.break_direction is None


def test_warming_up_no_break() -> None:
    assert _feed([100.0, 101.0]).break_direction is None   # ≤ lookback samples


# --------------------------------------------------------------------------- #
# scan2 ES_LEAD — ARM on /ES, CONFIRM on SPX
# --------------------------------------------------------------------------- #

def _state(prev_close: float, cur_close: float) -> MarketState:
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(prev_close))
    # small-range current bar so the rejection trigger can't pre-empt ES_LEAD
    st.push_bar(_bar(cur_close, h=cur_close + 0.5, l=cur_close - 0.5))
    return st


def test_es_lead_fires_when_spx_confirms() -> None:
    st = _state(7350.0, 7352.0)               # SPX +2 up, ≥ confirm_min
    cand = detect_candidate(st, _gex(7352.0), es_arm=Direction.BULLISH)
    assert cand is not None and cand.trigger is TriggerType.ES_LEAD


def test_es_lead_quiet_when_spx_does_not_confirm() -> None:
    st = _state(7350.0, 7350.0)               # SPX flat → no confirmation
    assert detect_candidate(st, _gex(7350.0), es_arm=Direction.BULLISH) is None


def test_es_lead_quiet_without_arm() -> None:
    st = _state(7350.0, 7352.0)
    assert detect_candidate(st, _gex(7352.0), es_arm=None) is None   # default path unchanged


def test_regime_flip_outranks_es_lead() -> None:
    # Zero-Γ at 7350; prev below, cur above → a regime flip, which ranks first.
    st = _state(7348.0, 7352.0)
    cand = detect_candidate(st, _gex(7352.0, zero_gamma=7350.0), es_arm=Direction.BULLISH)
    assert cand is not None and cand.trigger is TriggerType.REGIME_FLIP


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
