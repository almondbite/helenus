"""
Offline tests for engine/scan2.py — the candidate gate and key-level grid.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_scan2.py
    python tests/test_scan2.py

Covers the two range-day trigger fixes:
  * GEX walls + zero-gamma are injected into key_levels (so the gate fires on
    the structure the bot is built around, not just round numbers).
  * SWEEP_RECOVER works off real intra-bar high/low — a wick that pierces a
    level and reverses is caught even when the bar *closes* back on side.
"""

from __future__ import annotations

import datetime as dt

from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import (
    Bar,
    MarketState,
    TriggerType,
    detect_candidate,
)


def _bar(c: float, h: float | None = None, l: float | None = None, v: float = 100.0) -> Bar:
    """OHLC bar; high/low default to the close when not given."""
    return Bar(
        ts=dt.datetime(2026, 6, 22, 10, 0),
        open=c,
        high=c if h is None else h,
        low=c if l is None else l,
        close=c,
        volume=v,
    )


def _gex(
    spot: float,
    call_walls: list[tuple[float, float]] | None = None,
    put_walls: list[tuple[float, float]] | None = None,
    zero_gamma: float | None = None,
) -> GexProfile:
    import pandas as pd

    return GexProfile(
        spot=spot,
        total_net_gex=0.0,
        zero_gamma=zero_gamma,
        call_walls=call_walls or [],
        put_walls=put_walls or [],
        by_strike=pd.DataFrame(),
    )


# --------------------------------------------------------------------------- #
# B — GEX walls injected into key_levels
# --------------------------------------------------------------------------- #

def test_key_levels_without_gex_has_no_walls() -> None:
    st = MarketState(prior_close=7440.0)
    labels = {lv.label for lv in st.key_levels(7445.0)}
    assert not any(lbl.startswith(("Call Wall", "Put Wall")) for lbl in labels)
    assert "Zero-Γ" not in labels


def test_key_levels_injects_walls_and_zero_gamma() -> None:
    st = MarketState(prior_close=7440.0)
    gex = _gex(
        spot=7445.0,
        call_walls=[(7460.0, 5e8)],
        put_walls=[(7430.0, -4e8)],
        zero_gamma=7442.0,
    )
    by_label = {lv.label: lv for lv in st.key_levels(7445.0, gex)}
    assert by_label["Call Wall 7460"].price == 7460.0
    assert by_label["Put Wall 7430"].price == 7430.0
    assert by_label["Zero-Γ"].price == 7442.0
    # Walls/pivot must outweigh the round-number grid so confluence reads right.
    assert by_label["Call Wall 7460"].weight >= 0.85
    assert by_label["Zero-Γ"].weight >= 0.85


# --------------------------------------------------------------------------- #
# B — the gate now fires on a wall it could never have seen before
# --------------------------------------------------------------------------- #

def test_volume_cross_fires_on_a_put_wall_off_grid() -> None:
    # Put wall at 7452 (off the 10-pt grid). Price crosses it UP on 4x volume in
    # a band clear of round numbers, session extremes, and VWAP — so the wall is
    # the level that matches. Pre-fix it wasn't a crossable level at all.
    st = MarketState()
    st.push_bar(_bar(7462.0, h=7462.0, l=7444.0, v=100.0))  # session high 7462
    st.push_bar(_bar(7432.0, h=7448.0, l=7432.0, v=100.0))  # session low 7432
    for _ in range(19):
        st.push_bar(_bar(7445.0, v=100.0))                   # VWAP gravity ~7447
    st.push_bar(_bar(7451.0, v=100.0))                       # prev: just below wall
    st.push_bar(_bar(7453.0, v=400.0))                       # cur: crosses 7452 up

    cand = detect_candidate(st, _gex(spot=7453.0, put_walls=[(7452.0, -4e8)]))
    assert cand is not None
    assert cand.trigger is TriggerType.VOLUME_CONFIRMATION
    assert "Put Wall 7452" in cand.reason


# --------------------------------------------------------------------------- #
# A — SWEEP_RECOVER off real intra-bar high/low
# --------------------------------------------------------------------------- #

def test_sweep_recover_uses_intrabar_low() -> None:
    # before closes above the level; the pierce bar *wicks* below it but closes
    # back above; the next bar holds above. With real lows this is a sweep.
    st = MarketState()
    level = 7440.0  # round-number level on the grid
    st.push_bar(_bar(7443.0))                       # before: above
    st.push_bar(_bar(7442.0, l=7437.0))             # pierce: wick to 7437, close 7442
    st.push_bar(_bar(7444.0))                       # current: reclaimed

    cand = detect_candidate(st, _gex(spot=7444.0))
    assert cand is not None
    assert cand.trigger is TriggerType.SWEEP_RECOVER
    assert "below" in cand.reason


def test_no_sweep_when_low_never_pierces() -> None:
    # Same closes, but the pierce bar's low never reaches the level — the wick
    # that used to be invisible (high==low==close) is what makes or breaks this.
    st = MarketState()
    st.push_bar(_bar(7443.0))
    st.push_bar(_bar(7442.0, l=7441.0))             # low 7441 > level 7440: no pierce
    st.push_bar(_bar(7444.0))

    cand = detect_candidate(st, _gex(spot=7444.0))
    assert cand is None


# --------------------------------------------------------------------------- #
# LEVEL_REJECTION — touch-and-reverse at a weighty level
# --------------------------------------------------------------------------- #

def test_rejection_off_session_low_support() -> None:
    # Reproduces today's 10:55 floor bounce: bar wicks down to the session low
    # (~7461) and closes ~15pts higher. No deep pierce, so SWEEP_RECOVER can't
    # see it — LEVEL_REJECTION should.
    st = MarketState()
    st.push_bar(_bar(7475.0, l=7461.0))             # earlier bar sets the floor
    st.push_bar(_bar(7470.0))                       # context bar
    st.push_bar(_bar(7475.8, h=7477.3, l=7461.0))   # retest 7461, close 7475.8

    cand = detect_candidate(st, _gex(spot=7475.8))
    assert cand is not None
    assert cand.trigger is TriggerType.LEVEL_REJECTION
    assert "support" in cand.reason


def test_rejection_off_session_high_resistance() -> None:
    # Tag an established session high (7530) and close well off it.
    st = MarketState()
    st.push_bar(_bar(7525.0, h=7530.0))             # earlier bar sets the high
    st.push_bar(_bar(7521.9, h=7530.0, l=7520.0))   # retest 7530, close 7521.9

    cand = detect_candidate(st, _gex(spot=7521.9))
    assert cand is not None
    assert cand.trigger is TriggerType.LEVEL_REJECTION
    assert "resistance" in cand.reason


def test_no_rejection_at_interior_wall_pin() -> None:
    # A GEX wall sitting in the MIDDLE of the established range (price pinning it
    # all session) must not fire a rejection — that's the range-day noise the
    # edge gate exists to kill. 7475 is ~15 above the floor and ~55 below the high.
    st = MarketState()
    st.push_bar(_bar(7530.0, h=7530.0, l=7460.0))   # range floor 7460, ceiling 7530
    st.push_bar(_bar(7490.0))
    st.push_bar(_bar(7480.0, h=7481.0, l=7475.0))    # taps the 7475 wall, bounces

    gex = _gex(spot=7480.0, put_walls=[(7475.0, -4e8)])
    assert detect_candidate(st, gex) is None


def test_no_rejection_on_minor_round_chop() -> None:
    # Small wick that tags only a minor round number (weight 0.35) must stay
    # quiet — this is the midday-noise case we don't want alerting.
    st = MarketState()
    st.push_bar(_bar(7474.0))
    st.push_bar(_bar(7475.0, h=7476.0, l=7470.0))   # touches Round 7470 (minor)

    cand = detect_candidate(st, _gex(spot=7475.0))
    assert cand is None


def test_rejection_needs_a_real_wick() -> None:
    # Tags the session low but closes right at the low (no rejection) -> quiet.
    st = MarketState()
    st.push_bar(_bar(7475.0, h=7480.0, l=7461.0))   # floor 7461, ceiling 7480
    st.push_bar(_bar(7468.0))
    st.push_bar(_bar(7461.8, h=7464.0, l=7461.0))   # retests 7461, closes at the low

    cand = detect_candidate(st, _gex(spot=7461.8))
    assert cand is None


# --------------------------------------------------------------------------- #
# Cold-start volume baseline
# --------------------------------------------------------------------------- #

def test_volume_baseline_warms_up_early() -> None:
    st = MarketState()
    for _ in range(3):
        st.push_bar(_bar(7475.0, v=100.0))
    assert st.volume_baseline() != st.volume_baseline()  # NaN: < min priors
    for _ in range(4):
        st.push_bar(_bar(7475.0, v=100.0))
    # Now 7 bars (>= min_baseline_bars priors) -> a real ratio without waiting 20.
    assert st.volume_baseline() == 100.0
    st.push_bar(_bar(7475.0, v=300.0))
    assert abs(st.volume_ratio() - 3.0) < 1e-9


# --------------------------------------------------------------------------- #
# REGIME_FLIP — spot crosses the zero-gamma pivot
# --------------------------------------------------------------------------- #

def test_regime_flip_crossing_up() -> None:
    st = MarketState()
    st.push_bar(_bar(7405.0))                 # below zero-Γ
    st.push_bar(_bar(7415.0))                 # crosses up through 7410
    cand = detect_candidate(st, _gex(spot=7415.0, zero_gamma=7410.0))
    assert cand is not None
    assert cand.trigger is TriggerType.REGIME_FLIP
    assert "up" in cand.reason and "POSITIVE_GAMMA" in cand.reason


def test_regime_flip_crossing_down() -> None:
    st = MarketState()
    st.push_bar(_bar(7415.0))
    st.push_bar(_bar(7405.0))                 # crosses down through 7410
    cand = detect_candidate(st, _gex(spot=7405.0, zero_gamma=7410.0))
    assert cand is not None
    assert cand.trigger is TriggerType.REGIME_FLIP
    assert "down" in cand.reason and "NEGATIVE_GAMMA" in cand.reason


def test_no_regime_flip_without_a_cross() -> None:
    st = MarketState()
    st.push_bar(_bar(7420.0))
    st.push_bar(_bar(7425.0))                 # both stay above zero-Γ
    cand = detect_candidate(st, _gex(spot=7425.0, zero_gamma=7410.0))
    assert cand is None or cand.trigger is not TriggerType.REGIME_FLIP


# --------------------------------------------------------------------------- #
# RANGE_EXPANSION — a directional thrust between levels
# --------------------------------------------------------------------------- #

def test_range_expansion_on_a_thrust() -> None:
    st = MarketState()
    for _ in range(10):                       # calm bars -> low ATR (~1.0)
        st.push_bar(_bar(7500.0, h=7500.5, l=7499.5))
    for px in (7497.0, 7493.0, 7489.0, 7486.0, 7483.0):  # ~17pt drop over 5 bars
        st.push_bar(_bar(px, h=px + 0.5, l=px - 0.5))
    cand = detect_candidate(st, _gex(spot=7483.0, zero_gamma=7600.0))
    assert cand is not None
    assert cand.trigger is TriggerType.RANGE_EXPANSION
    assert "down-thrust" in cand.reason


def test_no_expansion_on_chop() -> None:
    st = MarketState()
    for i in range(15):                       # oscillate ±1, net displacement ~0
        px = 7500.0 + (1.0 if i % 2 else -1.0)
        st.push_bar(_bar(px, h=px + 1.0, l=px - 1.0))
    cand = detect_candidate(st, _gex(spot=st.bars[-1].close, zero_gamma=7600.0))
    assert cand is None or cand.trigger is not TriggerType.RANGE_EXPANSION


# --------------------------------------------------------------------------- #
# VWAP
# --------------------------------------------------------------------------- #

def test_vwap_weights_typical_price_by_volume() -> None:
    st = MarketState()
    st.push_bar(_bar(100.0, h=110.0, l=90.0, v=100.0))   # typical 100, vol 100
    st.push_bar(_bar(200.0, h=220.0, l=180.0, v=300.0))  # typical 200, vol 300
    # (100*100 + 200*300) / (100+300) = 70000/400 = 175
    assert abs(st.vwap() - 175.0) < 1e-9


def test_vwap_is_nan_without_volume() -> None:
    st = MarketState()
    st.push_bar(_bar(100.0, v=0.0))
    assert st.vwap() != st.vwap()  # NaN


def test_vwap_is_a_weighted_key_level() -> None:
    st = MarketState()
    st.push_bar(_bar(100.0, h=110.0, l=90.0, v=100.0))
    by_label = {lv.label: lv for lv in st.key_levels(100.0)}
    assert "VWAP" in by_label
    assert by_label["VWAP"].weight == 0.85
    assert abs(by_label["VWAP"].price - 100.0) < 1e-9


def test_no_vwap_level_before_any_volume() -> None:
    st = MarketState()
    st.push_bar(_bar(100.0, v=0.0))
    assert "VWAP" not in {lv.label for lv in st.key_levels(100.0)}


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
