"""
Offline tests for engine/cumdelta.py — /ES cumulative-delta exhaustion.

No socket, no network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_cumdelta.py
    python tests/test_cumdelta.py

Covers the pure pieces: the quote/tick-rule tick classifier, cumulative-delta
accumulation across canned ticks (incl. the daily-reset guard), and the two
absorption reads that give the veto + the reversal arm:
  * BEARISH absorption — price prints a new high on a LOWER cumulative-delta high
    (buyers absorbed → up-move spent → veto longs, arm shorts).
  * BULLISH absorption — the 7340-7350 case: price re-tags the session low while
    cumulative delta holds ABOVE its low-water mark (sellers absorbed → veto shorts).
Plus the scan2 gate emitting a CD_REVERSAL candidate only at the structural level.
"""

from __future__ import annotations

import datetime as dt

from helenus.config import CumDeltaConfig
from helenus.engine.cumdelta import (
    CumDeltaReading,
    CumDeltaTracker,
    classify_tick,
)
from helenus.engine.scan2 import (
    Bar,
    Direction,
    MarketState,
    TriggerType,
    detect_candidate,
)
from helenus.engine.gex import GexProfile


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_CFG = CumDeltaConfig(enabled=True, min_divergence=100.0, retag_tolerance_pts=2.0)


class _Feed:
    """Drives a tracker with explicit buy/sell aggressor ticks, tracking the
    running cumulative /ES volume so each tick carries the right interval delta."""

    def __init__(
        self, cfg: CumDeltaConfig = _CFG, start_vol: float = 1000.0, start_price: float = 100.0
    ) -> None:
        self.t = CumDeltaTracker(cfg)
        self.vol = start_vol
        self.t.observe(
            last=start_price, bid=start_price - 0.25, ask=start_price, total_volume=self.vol
        )  # baseline

    def buy(self, price: float, size: float) -> None:
        self.vol += size
        self.t.observe(last=price, bid=price - 0.25, ask=price, total_volume=self.vol)

    def sell(self, price: float, size: float) -> None:
        self.vol += size
        self.t.observe(last=price, bid=price, ask=price + 0.25, total_volume=self.vol)


def _gex(spot: float) -> GexProfile:
    import pandas as pd

    return GexProfile(
        spot=spot, total_net_gex=0.0, zero_gamma=None,
        call_walls=[], put_walls=[], by_strike=pd.DataFrame(),
    )


def _bar(c: float, h: float | None = None, l: float | None = None, v: float = 100.0) -> Bar:
    return Bar(
        ts=dt.datetime(2026, 6, 24, 14, 0),
        open=c, high=c if h is None else h, low=c if l is None else l, close=c, volume=v,
    )


# --------------------------------------------------------------------------- #
# classify_tick — quote rule + tick-rule fallback
# --------------------------------------------------------------------------- #

def test_classify_quote_rule() -> None:
    assert classify_tick(100.0, 99.5, bid=99.5, ask=100.0, vol_delta=50) == 50.0   # at offer → buy
    assert classify_tick(99.0, 99.5, bid=99.0, ask=99.5, vol_delta=50) == -50.0    # at bid → sell


def test_classify_tick_rule_inside_spread() -> None:
    # Inside the spread → fall back to the tick rule (uptick buy / downtick sell).
    assert classify_tick(99.6, 99.5, bid=99.0, ask=100.0, vol_delta=40) == 40.0
    assert classify_tick(99.4, 99.5, bid=99.0, ask=100.0, vol_delta=40) == -40.0


def test_classify_flat_tick_carries_prior_sign() -> None:
    # Flat tick, no quote help → carry the prior aggressor sign.
    assert classify_tick(99.5, 99.5, bid=99.0, ask=100.0, vol_delta=40, prev_sign=-1.0) == -40.0


def test_classify_no_volume_is_zero() -> None:
    assert classify_tick(100.0, 99.0, bid=99.0, ask=100.0, vol_delta=0) == 0.0


# --------------------------------------------------------------------------- #
# Cumulative-delta accumulation + the daily-reset guard
# --------------------------------------------------------------------------- #

def test_cum_delta_accumulates() -> None:
    f = _Feed()
    f.buy(100.0, 500)      # +500
    f.sell(99.0, 200)      # -200
    f.buy(99.5, 100)       # inside spread, downtick? 99.5>99 uptick → +100? bid99.25 ask99.5 → at ask buy +100
    r = f.t.reading()
    assert r.cum_delta == 400.0


def test_daily_reset_starts_fresh_session() -> None:
    f = _Feed()
    f.buy(100.0, 800)
    assert f.t.reading().cum_delta == 800.0
    # A sharp drop in cumulative volume = a new session → reset, not a clamp.
    f.t.observe(last=100.0, bid=99.75, ask=100.0, total_volume=10.0)
    r = f.t.reading()
    assert r.cum_delta == 0.0
    assert r.session_high == 100.0 and r.session_low == 100.0


# --------------------------------------------------------------------------- #
# Bearish absorption — new high on a lower cumulative-delta high
# --------------------------------------------------------------------------- #

def test_bearish_absorption_vetoes_longs_arms_shorts() -> None:
    f = _Feed()
    f.buy(100.0, 500)      # cum 500 at the 100 high → cd_at_high 500
    f.sell(96.0, 200)      # cum 300, price pulls away from the high (low 96)
    f.buy(102.0, 100)      # cum 400 — a NEW high (102) but on a weaker delta high
    r = f.t.reading()
    assert r.bearish_absorption is True
    assert r.bullish_absorption is False
    assert r.divergence == 100.0          # cd_at_high 500 − cum 400
    assert r.arm_direction is Direction.BEARISH
    assert r.veto_direction is Direction.BULLISH
    assert r.vetoes(Direction.BULLISH) is True
    assert r.vetoes(Direction.BEARISH) is False


# --------------------------------------------------------------------------- #
# Bullish absorption — the 7340-7350 held-shelf case
# --------------------------------------------------------------------------- #

def test_bullish_absorption_vetoes_shorts_arms_longs() -> None:
    f = _Feed(start_vol=1000.0, start_price=7356.0)
    f.sell(7350.0, 600)    # cum -600 at the 7350 low → cd_at_low -600 (strongest selling)
    f.buy(7356.0, 100)     # cum -500, price lifts off the shelf
    f.buy(7351.0, 200)     # cum -300, price RE-TAGS the held low but delta holds well above
    r = f.t.reading()
    assert r.bullish_absorption is True
    assert r.bearish_absorption is False
    assert r.divergence == 300.0          # cum -300 − cd_at_low -600
    assert r.arm_direction is Direction.BULLISH
    assert r.vetoes(Direction.BEARISH) is True   # the short cluster that bled
    assert r.vetoes(Direction.BULLISH) is False


def test_no_divergence_when_delta_confirms_the_extreme() -> None:
    f = _Feed()
    f.buy(101.0, 500)      # new high on rising delta
    f.buy(102.0, 500)      # another new high on still-rising delta → confirmation
    r = f.t.reading()
    assert r.bearish_absorption is False
    assert r.bullish_absorption is False
    assert r.arm_direction is None
    assert r.vetoes(Direction.BULLISH) is False


# --------------------------------------------------------------------------- #
# scan2 gate — the CD_REVERSAL arm only at the structural level
# --------------------------------------------------------------------------- #

def _bull_reading(low: float) -> CumDeltaReading:
    return CumDeltaReading(
        cum_delta=-300.0, session_high=low + 12, session_low=low,
        cd_at_high=None, cd_at_low=-600.0,
        bullish_absorption=True, bearish_absorption=False, divergence=300.0, note="absorbed",
    )


def test_gate_arms_cd_reversal_at_the_session_low() -> None:
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7350.0, l=7350.0))   # establishes the session low
    st.push_bar(_bar(7352.0, l=7351.0))   # current close 7352 — within arm_edge of the low
    cand = detect_candidate(st, _gex(7352.0), cumdelta=_bull_reading(7350.0))
    assert cand is not None
    assert cand.trigger is TriggerType.CD_REVERSAL
    assert cand.level is not None and cand.level.label == "Session Low"


def test_gate_does_not_arm_interior_absorption() -> None:
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7350.0, l=7350.0))   # session low 7350
    st.push_bar(_bar(7380.0))             # price 30pt off the low — interior, no arm
    cand = detect_candidate(st, _gex(7380.0), cumdelta=_bull_reading(7350.0))
    assert cand is None


def test_gate_unchanged_without_cumdelta() -> None:
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7350.0, l=7350.0))
    st.push_bar(_bar(7352.0, l=7351.0))
    assert detect_candidate(st, _gex(7352.0)) is None     # default cumdelta=None → no change


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
