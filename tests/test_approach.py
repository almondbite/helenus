"""
Offline tests for engine/scan2.py:detect_approach — the anticipatory arm (stage 1).

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_approach.py
    python tests/test_approach.py

Covers the WATCH arm: it fires when price is APPROACHING a reaction zone with
aligned structural context (and only then), respects the edge gate (no interior
pins) and the heading (no arming while moving away), scales the confidence
pre-load with the number of aligned context legs, and the pure pre-load helper
raises an aligned entry's confidence (clamped) while leaving a misaligned one be.

Uses default CONFIG.approach (approach_pts=7, confidence_preload=8,
require_context=True) and CONFIG.scan (edge_proximity_pts=10, rejection_min_weight
0.7). Trend is None at <20 bars, so the trend gate never opposes these fixtures.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from helenus.engine.charm import CharmProfile
from helenus.engine.gex import GexProfile
from helenus.engine.gexmap import GexMapTracker
from helenus.engine.scan2 import (
    ApproachArm,
    Bar,
    Direction,
    KeyLevel,
    MarketState,
    detect_approach,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _bar(c: float, h: float | None = None, l: float | None = None, v: float = 100.0) -> Bar:
    return Bar(
        ts=dt.datetime(2026, 6, 24, 14, 0),
        open=c, high=c if h is None else h, low=c if l is None else l, close=c, volume=v,
    )


def _gex(
    spot: float,
    call_walls: list[tuple[float, float]] | None = None,
    put_walls: list[tuple[float, float]] | None = None,
    zero_gamma: float | None = None,
    total_net_gex: float = 0.0,
) -> GexProfile:
    return GexProfile(
        spot=spot, total_net_gex=total_net_gex, zero_gamma=zero_gamma,
        call_walls=call_walls or [], put_walls=put_walls or [], by_strike=pd.DataFrame(),
    )


def _charm(
    bias: str = "SUPPORTIVE", minutes: float = 90.0,
    support_walls: list[tuple[float, float]] | None = None,
    resistance_walls: list[tuple[float, float]] | None = None,
) -> CharmProfile:
    if bias == "SUPPORTIVE":
        ps, co = 1000.0, 100.0
    elif bias == "OVERHEAD":
        ps, co = 100.0, 1000.0
    else:
        ps, co = 100.0, 100.0      # BALANCED
    return CharmProfile(
        spot=0.0, minutes_to_expiry=minutes, net_charm=ps - co,
        put_support=ps, call_overhead=co,
        support_walls=support_walls or [], resistance_walls=resistance_walls or [],
        by_strike=pd.DataFrame(),
    )


def _descending() -> MarketState:
    """Price stepping DOWN toward ~7352, session low ~7352."""
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7360.0, l=7358.0))
    st.push_bar(_bar(7356.0, l=7355.0))
    st.push_bar(_bar(7353.0, l=7352.0))
    return st


def _ascending() -> MarketState:
    """Price stepping UP toward ~7357."""
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7351.0, l=7350.0))
    st.push_bar(_bar(7354.0, l=7352.0))
    st.push_bar(_bar(7357.0, l=7355.0))
    return st


# --------------------------------------------------------------------------- #
# Arms with context
# --------------------------------------------------------------------------- #

def test_arms_bullish_into_charm_support() -> None:
    # Descending into charm support at 7350 with HIGH supportive charm → bullish.
    st = _descending()
    charm = _charm("SUPPORTIVE", minutes=90, support_walls=[(7350.0, -1e6)])
    arm = detect_approach(st, _gex(7353.0), charm)   # UNKNOWN regime → only the charm leg
    assert arm is not None
    assert arm.direction is Direction.BULLISH
    assert "support" in arm.reason
    assert arm.confidence_preload == 4.0             # 1 aligned leg → half of 8


def test_arms_bearish_into_call_wall_in_positive_gamma() -> None:
    # Ascending into a call wall from below in positive gamma → bearish fade.
    st = _ascending()
    gex = _gex(7357.0, call_walls=[(7360.0, 5e8)], zero_gamma=7350.0, total_net_gex=5e8)
    assert gex.regime == "POSITIVE_GAMMA"
    arm = detect_approach(st, gex, charm=None)
    assert arm is not None
    assert arm.direction is Direction.BEARISH
    assert arm.level.label.startswith("Call Wall")
    assert arm.confidence_preload == 4.0             # positive-gamma leg only


def test_preload_scales_with_aligned_legs() -> None:
    # Same bullish approach but now ALSO in positive gamma → two legs → full preload.
    st = _descending()
    charm = _charm("SUPPORTIVE", minutes=90, support_walls=[(7350.0, -1e6)])
    gex = _gex(7353.0, zero_gamma=7345.0, total_net_gex=5e8)   # POSITIVE_GAMMA
    assert gex.regime == "POSITIVE_GAMMA"
    arm = detect_approach(st, gex, charm)
    assert arm is not None and arm.direction is Direction.BULLISH
    assert arm.confidence_preload == 8.0             # 2 legs → full


# --------------------------------------------------------------------------- #
# Guards — no context, interior pin, moving away
# --------------------------------------------------------------------------- #

def test_no_arm_without_context() -> None:
    # Same descending approach but BALANCED charm + UNKNOWN regime → no legs.
    st = _descending()
    charm = _charm("BALANCED")
    assert detect_approach(st, _gex(7353.0), charm) is None


def test_no_arm_on_interior_pin() -> None:
    # Charm support at 7353 is NOT near the session low (a 7300 spike), so the edge
    # gate rejects it — interior price pinning a wall must not arm.
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7370.0, l=7300.0))   # spike low → session low 7300
    st.push_bar(_bar(7360.0, l=7358.0))
    st.push_bar(_bar(7356.0, l=7354.0))   # cur heading down, far above the 7300 low
    charm = _charm("SUPPORTIVE", minutes=90, support_walls=[(7353.0, -1e6)])
    assert detect_approach(st, _gex(7356.0), charm) is None


def test_no_arm_when_moving_away_from_support() -> None:
    # Price ascending away from support below, nothing above → no arm.
    st = _ascending()
    charm = _charm("SUPPORTIVE", minutes=90, support_walls=[(7350.0, -1e6)])
    assert detect_approach(st, _gex(7357.0), charm) is None


# --------------------------------------------------------------------------- #
# Stage 4 — the GEX-map-driven arm (gex_map overrides the legacy grid scan)
# --------------------------------------------------------------------------- #

def _map(spot_seed: float, spot_now: float, **gex_kw) -> "object":
    """Build a GexMapState heading from spot_seed → spot_now (two tracker polls)."""
    tr = GexMapTracker()
    tr.update(_gex(spot_seed, **gex_kw))
    return tr.update(_gex(spot_now, **gex_kw))


def test_map_arm_on_approaching_wall() -> None:
    # Rising into a call wall at 7360 (4pt up) in positive gamma → the map arms the
    # BEARISH fade, level = the wall, reason carries the prior, full preload (PROMOTE).
    gex_map = _map(
        7352.0, 7356.0, call_walls=[(7360.0, 5e8)], put_walls=[(7340.0, -5e8)],
        zero_gamma=7350.0, total_net_gex=5e8,
    )
    assert gex_map.position_state == "APPROACHING_WALL"
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7352.0))
    st.push_bar(_bar(7356.0))   # heading up
    arm = detect_approach(st, _gex(7356.0), charm=None, gex_map=gex_map)
    assert arm is not None
    assert arm.direction is Direction.BEARISH
    assert arm.level.label == "Call Wall 7360"
    assert "mean-reversion" in arm.reason
    assert arm.confidence_preload == 8.0


def test_map_arm_on_approaching_flip() -> None:
    # Rising into the zero-Γ flip at 7360 (4pt up), walls far → APPROACHING_FLIP, the
    # map arms BULLISH momentum through the pivot, level = Zero-Γ.
    gex_map = _map(
        7352.0, 7356.0, call_walls=[(7380.0, 5e8)], put_walls=[(7330.0, -5e8)],
        zero_gamma=7360.0, total_net_gex=-5e8,
    )
    assert gex_map.position_state == "APPROACHING_FLIP"
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7352.0))
    st.push_bar(_bar(7356.0))
    arm = detect_approach(st, _gex(7356.0), charm=None, gex_map=gex_map)
    assert arm is not None
    assert arm.direction is Direction.BULLISH
    assert arm.level.label == "Zero-Γ"


def test_no_map_arm_in_open_space_falls_back() -> None:
    # Walls 25pt away on both sides → IN_OPEN_SPACE, the map arms nothing, and with no
    # charm/flow context the legacy scan also stays quiet → None (no spurious arm).
    gex_map = _map(
        7351.0, 7355.0, call_walls=[(7380.0, 5e8)], put_walls=[(7330.0, -5e8)],
        zero_gamma=7350.0, total_net_gex=5e8,
    )
    assert gex_map.position_state == "IN_OPEN_SPACE"
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7351.0))
    st.push_bar(_bar(7355.0))
    assert detect_approach(st, _gex(7355.0), charm=None, gex_map=gex_map) is None


# --------------------------------------------------------------------------- #
# The pure pre-load helper
# --------------------------------------------------------------------------- #

def _arm(direction: Direction, preload: float) -> ApproachArm:
    return ApproachArm(
        direction=direction,
        level=KeyLevel(7350.0, "Charm Support 7350", 0.8),
        distance_pts=3.0, confidence_preload=preload, reason="test",
    )


def test_preload_raises_aligned_confidence() -> None:
    arm = _arm(Direction.BULLISH, 8.0)
    conf, note = arm.preload(Direction.BULLISH, 50.0)
    assert conf == 58.0 and note is not None and "Pre-armed" in note


def test_preload_clamps_at_95() -> None:
    arm = _arm(Direction.BULLISH, 8.0)
    conf, _note = arm.preload(Direction.BULLISH, 92.0)
    assert conf == 95.0


def test_preload_ignores_misaligned_direction() -> None:
    arm = _arm(Direction.BULLISH, 8.0)
    conf, note = arm.preload(Direction.BEARISH, 50.0)
    assert conf == 50.0 and note is None


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
