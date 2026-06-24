"""
Offline tests for engine/orb.py — the Opening Range Breakout engine.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_orb.py
    python tests/test_orb.py

Asserts the mechanics: the range builds during the opening window and locks after
it; a CLOSE beyond the locked range with volume + VWAP alignment fires once per
side with the right R-multiple plan; the volume / VWAP filters gate fakeouts; the
range resets daily; and `warm` rebuilds the range without re-alerting.
"""

from __future__ import annotations

import datetime as dt

from helenus.config import CONFIG
from helenus.data.schwab_feed import ET
from helenus.engine.orb import ORBEngine
from helenus.engine.scan2 import Bar, Direction

_OPEN = dt.datetime(2026, 6, 22, 9, 30, tzinfo=ET)


def _bar(h, l, c, m, v=1000.0, day=_OPEN):
    return Bar(ts=day + dt.timedelta(minutes=m), open=float(c), high=float(h),
               low=float(l), close=float(c), volume=v)


def _build_window(eng: ORBEngine, hi=5005.0, lo=4995.0):
    """Feed the 15 one-minute bars of the opening window so the range locks at
    [lo, hi] (a 10-pt range by default)."""
    last = None
    for m in range(CONFIG.orb.window_min):
        h = hi if m == 3 else 5002.0
        l = lo if m == 7 else 4998.0
        last = eng.on_bar(_bar(h, l, 5000.0, m), vwap=5000.0, volume_ratio=1.0)
    return last


# --------------------------------------------------------------------------- #
# Range construction
# --------------------------------------------------------------------------- #

def test_range_builds_in_window_then_locks() -> None:
    eng = ORBEngine()
    last = _build_window(eng)
    assert last.in_window and not last.locked
    assert last.range_high == 5005.0 and last.range_low == 4995.0
    assert last.range_pts == 10.0
    # First bar after the window: locked, no breakout (inside the range).
    after = eng.on_bar(_bar(5004, 4996, 5000, 15), vwap=5000.0, volume_ratio=1.0)
    assert after.locked and not after.in_window and after.direction is None


# --------------------------------------------------------------------------- #
# Confirmed breakout
# --------------------------------------------------------------------------- #

def test_long_breakout_with_filters_is_active() -> None:
    eng = ORBEngine()
    _build_window(eng)
    r = eng.on_bar(_bar(5008, 5006, 5007, 15), vwap=5000.0, volume_ratio=2.0)
    assert r.direction is Direction.BULLISH and r.active
    assert r.volume_ok and r.vwap_ok
    assert r.entry == 5005.0 and r.stop == 4995.0
    assert r.targets == [5015.0, 5025.0]     # +1R / +2R off a 10-pt range


def test_short_breakout_with_filters_is_active() -> None:
    eng = ORBEngine()
    _build_window(eng)
    r = eng.on_bar(_bar(4994, 4992, 4993, 15), vwap=5000.0, volume_ratio=2.0)
    assert r.direction is Direction.BEARISH and r.active
    assert r.entry == 4995.0 and r.stop == 5005.0
    assert r.targets == [4985.0, 4975.0]


def test_breakout_fires_once_per_side() -> None:
    eng = ORBEngine()
    _build_window(eng)
    first = eng.on_bar(_bar(5008, 5006, 5007, 15), vwap=5000.0, volume_ratio=2.0)
    second = eng.on_bar(_bar(5012, 5010, 5011, 16), vwap=5000.0, volume_ratio=2.0)
    assert first.active and not second.active


# --------------------------------------------------------------------------- #
# Fakeout filters
# --------------------------------------------------------------------------- #

def test_low_volume_breakout_is_filtered() -> None:
    eng = ORBEngine()
    _build_window(eng)
    r = eng.on_bar(_bar(5008, 5006, 5007, 15), vwap=5000.0, volume_ratio=1.0)
    assert r.direction is Direction.BULLISH and not r.volume_ok and not r.active


def test_long_below_vwap_is_filtered() -> None:
    eng = ORBEngine()
    _build_window(eng)
    # Close clears the range high but sits below VWAP → no institutional support.
    r = eng.on_bar(_bar(5008, 5006, 5007, 15), vwap=5010.0, volume_ratio=2.0)
    assert not r.vwap_ok and not r.active


# --------------------------------------------------------------------------- #
# Session handling
# --------------------------------------------------------------------------- #

def test_resets_on_a_new_day() -> None:
    eng = ORBEngine()
    _build_window(eng)
    eng.on_bar(_bar(5008, 5006, 5007, 15), vwap=5000.0, volume_ratio=2.0)
    day2 = dt.datetime(2026, 6, 23, 9, 30, tzinfo=ET)
    r = eng.on_bar(_bar(4001, 3999, 4000, 1, day=day2), vwap=4000.0, volume_ratio=1.0)
    assert r.in_window and r.range_high == 4001.0 and r.range_low == 3999.0


def test_warm_rebuilds_range_and_suppresses_already_broken() -> None:
    # Replay a full session where the long break already happened, then a fresh
    # break close must NOT re-fire (warm pre-set the fired flag).
    bars = []
    for m in range(CONFIG.orb.window_min):
        h = 5005.0 if m == 3 else 5002.0
        l = 4995.0 if m == 7 else 4998.0
        bars.append(_bar(h, l, 5000.0, m))
    bars.append(_bar(5008, 5006, 5007, 15))          # the breakout, pre-restart
    eng = ORBEngine()
    eng.warm(bars)
    again = eng.on_bar(_bar(5012, 5010, 5011, 16), vwap=5000.0, volume_ratio=2.0)
    assert again.locked and again.range_high == 5005.0
    assert not again.active                           # already fired before restart


def test_pre_open_bar_is_ignored() -> None:
    eng = ORBEngine()
    pre = dt.datetime(2026, 6, 22, 9, 0, tzinfo=ET)
    r = eng.on_bar(Bar(ts=pre, open=5000, high=5001, low=4999, close=5000, volume=1),
                   vwap=5000.0, volume_ratio=1.0)
    assert not r.locked and not r.in_window and r.range_high is None


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
