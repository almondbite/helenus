"""
Offline tests for engine/zones.py — the ported displacement zone lifecycle
(Task 4: 8-component spawn score, ACTIVE→TESTED→MITIGATED/BROKEN state machine
with distinct-entry touch counts and half-life age decay, FVG OPEN→CE→FILLED,
and the relation queries the approach-arm logic consumes).

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_zones.py
    python tests/test_zones.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from helenus.config import CONFIG
from helenus.engine.scan2 import Bar, MarketState
from helenus.engine.zones import (
    ACTIVE,
    BROKEN,
    FVG_CE,
    FVG_FILLED,
    FVG_OPEN,
    MITIGATED,
    TESTED,
    ZoneEngine,
    _convict,
    _norm_ratio,
)

CFG = CONFIG.zones
DISABLED = dataclasses.replace(CFG, enabled=False)

_T0 = dt.datetime(2026, 6, 24, 10, 0)


def _bar(i: int, c: float, *, o=None, h=None, l=None, v=100.0) -> Bar:
    return Bar(
        ts=_T0 + dt.timedelta(minutes=i),
        open=float(c if o is None else o),
        high=float(c if h is None else h),
        low=float(c if l is None else l),
        close=float(c),
        volume=v,
    )


def _calm(n: int, base: float = 100.0, start: int = 0) -> list[Bar]:
    out = []
    for i in range(n):
        up = i % 2 == 0
        o = base - 0.2 if up else base + 0.2
        c = base + 0.2 if up else base - 0.2
        out.append(_bar(start + i, c, o=o, h=base + 0.5, l=base - 0.5))
    return out


def _spawn_bull(i: int, base: float = 100.0, v: float = 500.0) -> Bar:
    """A bull displacement candle that clears the raw gate and the score
    threshold off the calm tape: body 6, range 7, close near the high."""
    return _bar(i, base + 6.0, o=base, h=base + 6.5, l=base - 0.5, v=v)


class _Runner:
    def __init__(self, cfg=None) -> None:
        self.eng = ZoneEngine(cfg or CFG)
        self.st = MarketState()

    def push(self, bar: Bar):
        self.st.push_bar(bar)
        return self.eng.on_bar(self.st)

    def feed(self, bars: list[Bar]):
        r = None
        for b in bars:
            r = self.push(b)
        return r


# --------------------------------------------------------------------------- #
# The Pine scoring helpers
# --------------------------------------------------------------------------- #

def test_norm_ratio_midpoint_and_saturation() -> None:
    assert _norm_ratio(1.0) == 0.5          # at the threshold
    assert _norm_ratio(2.0) == 1.0          # 2× threshold saturates
    assert _norm_ratio(0.0) == 0.0
    assert _norm_ratio(-3.0) == 0.0


def test_convict_bands() -> None:
    assert _convict(0.55, 0.55) == 0.5      # exactly at the threshold
    assert _convict(1.0, 0.55) == 1.0       # close at the extreme
    assert abs(_convict(0.275, 0.55) - 0.25) < 1e-9   # half-way below


# --------------------------------------------------------------------------- #
# Spawn: raw gate + 8-component score
# --------------------------------------------------------------------------- #

def test_spawn_on_a_qualifying_displacement() -> None:
    run = _Runner()
    r = run.feed(_calm(25) + [_spawn_bull(25)])
    assert r.spawned is not None
    z = r.spawned
    assert z.is_bull and z.state == ACTIVE
    assert z.bottom == 100.0 and z.top == 106.0          # body bounds
    # Body/ATR components saturate on a 6pt body vs a 0.4/1.0 baseline; the
    # three unsourceable components pin at 0.5 → the score lands mid-70s+.
    assert z.score >= CFG.min_score
    assert r.n_active == 1
    assert any("regime" in d for d in r.degraded)
    assert any("vrp" in d for d in r.degraded)


def test_no_spawn_below_min_score() -> None:
    strict = dataclasses.replace(CFG, min_score=99.0)
    run = _Runner(strict)
    r = run.feed(_calm(25) + [_spawn_bull(25)])
    assert r.spawned is None and len(r.zones) == 0


def test_no_spawn_on_a_weak_candle() -> None:
    run = _Runner()
    r = run.feed(_calm(26))                              # never clears the raw gate
    assert r.spawned is None and len(r.zones) == 0


def test_overlap_filter_skips_same_side_duplicates() -> None:
    run = _Runner()
    run.feed(_calm(25) + [_spawn_bull(25)])
    # Price falls back and prints another bull displacement over the SAME range.
    run.push(_bar(26, 100.5, o=101.0, h=101.2, l=100.3))
    r = run.push(_spawn_bull(27, base=100.5))
    assert r.spawned is None                              # overlapping live bull zone
    assert len([z for z in r.zones if z.state != BROKEN]) == 1


def test_fifo_cap_drops_the_oldest() -> None:
    capped = dataclasses.replace(CFG, max_zones=2, overlap_filter=False)
    run = _Runner(capped)
    run.feed(_calm(25))
    r1 = run.push(_spawn_bull(25, base=100.0))
    run.push(_bar(26, 120.0, o=119.6, h=120.4, l=119.5))  # drift up, stay out of zones
    r2 = run.push(_spawn_bull(27, base=120.0))
    run.push(_bar(28, 140.0, o=139.6, h=140.4, l=139.5))
    r3 = run.push(_spawn_bull(29, base=140.0))
    assert r1.spawned and r2.spawned and r3.spawned
    assert len(r3.zones) == 2
    assert min(z.bottom for z in r3.zones) == 120.0       # the 100-zone was shifted out


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #

def _spawned_runner() -> _Runner:
    run = _Runner()
    run.feed(_calm(25) + [_spawn_bull(25)])               # bull zone 100–106
    return run


def test_tested_on_entry_and_distinct_touch_counting() -> None:
    run = _spawned_runner()
    run.push(_bar(26, 108.0, o=107.6, h=108.4, l=107.5))  # clear of the zone
    r = run.push(_bar(27, 104.0, o=105.5, h=105.8, l=103.8))  # closes INSIDE
    z = r.zones[0]
    assert z.state == TESTED and z.touches == 1
    r = run.push(_bar(28, 104.5, o=104.0, h=104.8, l=103.9))  # still inside — same visit
    assert r.zones[0].touches == 1
    run.push(_bar(29, 108.0, o=107.6, h=108.5, l=107.4))  # leaves the zone
    r = run.push(_bar(30, 105.0, o=106.5, h=106.8, l=104.8))  # re-entry = a NEW touch
    assert r.zones[0].touches == 2


def test_mitigated_on_tag_and_close_back_outside() -> None:
    run = _spawned_runner()
    run.push(_bar(26, 108.0, o=107.6, h=108.4, l=107.5))
    r = run.push(_bar(27, 107.0, o=107.5, h=107.8, l=105.5))  # tags the top, closes above
    z = r.zones[0]
    assert z.state == MITIGATED and z.touches == 1


def test_broken_on_close_through_and_purged_next_bar() -> None:
    run = _spawned_runner()
    # A weak drift close below the bottom (small body/range so the break candle
    # is not itself a displacement that would spawn a bear zone).
    r = run.push(_bar(26, 99.6, o=100.3, h=100.4, l=99.4))
    assert r.zones[0].state == BROKEN
    assert r.n_active == 0
    r = run.push(_bar(27, 99.5, o=99.0, h=99.8, l=98.9))
    assert len(r.zones) == 0                                   # gone the bar after


def test_age_decay_halves_at_the_half_life() -> None:
    fast = dataclasses.replace(CFG, decay_bars=4)
    run = _Runner(fast)
    r = run.feed(_calm(25) + [_spawn_bull(25)])
    score = r.spawned.score
    for i in range(4):                                         # 4 bars of age
        r = run.push(_bar(26 + i, 108.0 + i, o=107.6 + i, h=108.4 + i, l=107.5 + i))
    z = r.zones[0]
    assert z.age_bars == 4
    assert abs(z.effective - round(score / 2.0, 1)) <= 0.1


# --------------------------------------------------------------------------- #
# FVG lifecycle: OPEN → CE(50%) → FILLED
# --------------------------------------------------------------------------- #

def test_fvg_open_ce_filled() -> None:
    run = _Runner()
    run.feed(_calm(10))
    # Gap up: this bar's low (102) > the high two back (100.5) → bull FVG 100.5–102.
    r = run.push(_bar(10, 103.0, o=102.5, h=103.4, l=102.0))
    assert len(r.fvgs) == 1
    f = r.fvgs[0]
    assert f.is_bull and f.state == FVG_OPEN
    assert f.bottom == 100.5 and f.top == 102.0
    # Dip to the midpoint (101.25) → CE.
    r = run.push(_bar(11, 102.8, o=103.0, h=103.1, l=101.2))
    assert r.fvgs[0].state == FVG_CE
    # Full fill (low ≤ bottom) → FILLED, then purged.
    r = run.push(_bar(12, 101.0, o=102.0, h=102.2, l=100.4))
    assert r.fvgs[0].state == FVG_FILLED
    r = run.push(_bar(13, 101.5, o=101.0, h=101.7, l=100.9))
    assert len(r.fvgs) == 0


# --------------------------------------------------------------------------- #
# Relation queries — the approach-arm surface
# --------------------------------------------------------------------------- #

def test_relation_approaching_testing_and_nearest() -> None:
    run = _spawned_runner()                                    # bull zone 100–106
    r = run.push(_bar(26, 110.0, o=109.6, h=110.4, l=109.5))   # 4pt above the top
    assert r.relation == "APPROACHING"
    assert r.relation_zone is not None and r.relation_zone.top == 106.0
    assert r.nearest_bull_below is not None
    r = run.push(_bar(27, 104.0, o=105.0, h=105.2, l=103.8))   # inside
    assert r.relation == "TESTING"
    r = run.push(_bar(28, 120.0, o=119.6, h=120.4, l=119.5))   # far away
    assert r.relation == "CLEAR"


def test_disabled_returns_none() -> None:
    run = _Runner(DISABLED)
    assert run.feed(_calm(5)) is None


def test_warm_rebuilds_the_same_zones() -> None:
    bars = _calm(25) + [_spawn_bull(25)]
    eng = ZoneEngine(CFG)
    eng.warm(bars)
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    st.push_bar(_bar(26, 107.0, o=106.4, h=107.4, l=106.3))
    r = eng.on_bar(st)
    assert len(r.zones) == 1 and r.zones[0].bottom == 100.0    # the backfilled zone


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
