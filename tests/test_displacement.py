"""
Offline tests for engine/displacement.py — the institutional-displacement read.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_displacement.py
    python tests/test_displacement.py

Asserts the structural contract: the displacement candle (big body / small wicks
/ high volume), the fair value gap (1st & 3rd wicks don't overlap), the market
structure shift (close past a recent swing), and the liquidity-sweep booster.
"""

from __future__ import annotations

import datetime as dt

from helenus.data.schwab_feed import ET
from helenus.engine.scan2 import Bar, Direction, MarketState
from helenus.engine.displacement import build_displacement

_T0 = dt.datetime(2026, 6, 22, 10, 0, tzinfo=ET)


def _b(o, h, l, c, v, i):
    return Bar(ts=_T0 + dt.timedelta(minutes=i), open=float(o), high=float(h),
               low=float(l), close=float(c), volume=float(v))


# A tight consolidation (floor ~4998, swing high `swing_top`), optionally a
# stop-hunt sweep below the floor, then a displacement candle (c2) launching from
# the floor and the bar that completes the FVG (c3). The launch lows stay at the
# floor so the move is NOT a sweep by default — `sweep=True` adds the stop hunt.
def _bars(*, swing_top=5001.0,
          c2=(4999, 5011, 4998, 5010, 5000),
          c3=(5010, 5015, 5003, 5012, 3000),
          sweep=False):
    seq = [(4999, 5001, 4998, 5000), (5000, 5001, 4999, 4999),
           (4999, 5000, 4998, 5000), (5000, swing_top, 4998, 5000),
           (5000, 5000, 4998, 4999)]
    bars, i = [], 0
    for j in range(13):
        o, h, l, c = seq[j % len(seq)]
        bars.append(_b(o, h, l, c, 1000, i)); i += 1
    bars.append(_b(4998, 4998, 4995, 4997, 1200, i) if sweep
                else _b(4999, 5000, 4999, 4999, 1100, i)); i += 1
    bars.append(_b(4999, 5000, 4998, 4999, 1100, i)); i += 1   # pre
    bars.append(_b(4999, 5000, 4998, 4999, 1100, i)); i += 1   # c1
    bars.append(_b(*c2, i)); i += 1                            # c2 = displacement
    bars.append(_b(*c3, i)); i += 1                            # c3 completes the FVG
    return bars


def _mirror(bars, pivot=5000.0):
    """Vertically flip the tape around `pivot` (high/low swap) — turns a bullish
    setup into its exact bearish analogue."""
    return [
        Bar(ts=b.ts, open=2 * pivot - b.open, high=2 * pivot - b.low,
            low=2 * pivot - b.high, close=2 * pivot - b.close, volume=b.volume)
        for b in bars
    ]


def _read(bars) -> "object":
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    return build_displacement(st)


# --------------------------------------------------------------------------- #
# The full setup
# --------------------------------------------------------------------------- #

def test_bullish_displacement_is_active() -> None:
    r = _read(_bars())
    assert r.direction is Direction.BULLISH
    assert r.detected and r.fvg and r.mss and r.active
    assert r.body_frac >= 0.6 and r.vol_ratio >= 1.5
    assert r.fvg_low is not None and r.fvg_high is not None and r.fvg_high > r.fvg_low
    assert r.mss_level is not None


def test_bearish_displacement_is_active() -> None:
    r = _read(_mirror(_bars()))
    assert r.direction is Direction.BEARISH
    assert r.detected and r.fvg and r.mss and r.active


# --------------------------------------------------------------------------- #
# Each pillar is necessary
# --------------------------------------------------------------------------- #

def test_no_fvg_when_wicks_overlap() -> None:
    # c3 drops back so its low overlaps c1's high → no imbalance.
    r = _read(_bars(c3=(5010, 5015, 4998, 5004, 3000)))
    assert r.detected and not r.fvg
    assert not r.active


def test_no_mss_when_close_holds_inside_structure() -> None:
    # Raise the consolidation high above the thrust's close → no structure break.
    r = _read(_bars(swing_top=5015.0))
    assert r.detected and not r.mss
    assert not r.active


def test_small_body_is_not_a_displacement() -> None:
    r = _read(_bars(c2=(5000, 5002, 4999, 5001, 5000)))
    assert not r.detected and not r.active


def test_large_wicks_disqualify_the_candle() -> None:
    # Same range, but the body is a small fraction of it (big wicks).
    r = _read(_bars(c2=(4997, 5012, 4990, 5004, 5000)))
    assert r.body_frac < 0.6
    assert not r.detected and not r.active


def test_low_volume_disqualifies_the_candle() -> None:
    r = _read(_bars(c2=(4999, 5011, 4998, 5010, 1000)))
    assert r.vol_ratio < 1.5
    assert not r.detected and not r.active


# --------------------------------------------------------------------------- #
# Sweep booster
# --------------------------------------------------------------------------- #

def test_sweep_is_a_booster_not_a_requirement() -> None:
    with_sweep = _read(_bars(sweep=True))
    without = _read(_bars(sweep=False))
    assert with_sweep.swept and with_sweep.swept_level is not None
    # No stop-hunt, but candle+FVG+MSS still carry it (require_sweep defaults off).
    assert not without.swept
    assert without.active


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def test_too_few_bars_is_inactive() -> None:
    st = MarketState()
    st.push_bar(_b(5000, 5001, 4999, 5000, 1000, 0))
    r = build_displacement(st)
    assert not r.active and r.direction is None


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
