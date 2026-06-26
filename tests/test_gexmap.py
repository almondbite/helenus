"""
Offline tests for engine/gexmap.py — the persistent GEX map + the directional prior.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_gexmap.py
    python tests/test_gexmap.py

Chains are hand-built (the tests/test_gex.py style). With gamma=0.1 everywhere,
multiplier=100 and spot≈100, each strike's net_gex is (callOI - putOI) * 1000, so the
walls and zero-gamma flip are computable by hand.
"""

from __future__ import annotations

import math

from helenus.engine.gex import build_profile
from helenus.engine.gexmap import GexMapTracker
from helenus.engine.scan2 import Direction


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def _contract(strike: float, call_oi: float, put_oi: float) -> tuple[dict, dict]:
    g = 0.10
    call = {"strikePrice": strike, "openInterest": call_oi, "gamma": g, "totalVolume": 10}
    put = {"strikePrice": strike, "openInterest": put_oi, "gamma": g, "totalVolume": 10}
    return call, put


def _chain(spot: float, rows: list[tuple[float, float, float]]) -> dict:
    """rows = [(strike, call_oi, put_oi)]. net_gex per strike = (call_oi-put_oi)*1000."""
    calls, puts = [], []
    for strike, c_oi, p_oi in rows:
        c, p = _contract(strike, c_oi, p_oi)
        calls.append(c)
        puts.append(p)

    def side_map(contracts: list[dict]) -> dict:
        strikes: dict[str, list[dict]] = {}
        for c in contracts:
            strikes.setdefault(f"{c['strikePrice']:.1f}", []).append(c)
        return {"2026-06-19:0": strikes}

    return {
        "underlying": {"mark": spot},
        "callExpDateMap": side_map(calls),
        "putExpDateMap": side_map(puts),
    }


# Canonical "between walls, positive gamma" chain: a put wall at 90 (net -20k) and a
# call wall at 110 (net +40k), nothing near spot. total +20k (positive), zero-Γ = 100.
# A 20pt-wide envelope leaves genuine open space (>3pt off both walls AND the flip).
_ROWS = [(90.0, 10, 30), (110.0, 50, 10)]


# --------------------------------------------------------------------------- #
# Stage 1 — the spatial state
# --------------------------------------------------------------------------- #

def test_open_space_between_walls() -> None:
    # spot 104: nearer the 110 call wall (6pt) than the 90 put wall (14pt); 4pt off
    # the zero-Γ flip (100) — genuinely in open space.
    st = GexMapTracker().update(build_profile(_chain(104.0, _ROWS)))
    assert st.position_state == "IN_OPEN_SPACE"
    assert st.nearest_wall_above[0] == 110.0
    assert st.nearest_wall_below[0] == 90.0
    assert _approx(st.dist_above_pts, 6.0)
    assert _approx(st.dist_below_pts, 14.0)
    assert st.envelope == (90.0, 110.0)
    assert "PutWall 90" in st.cell and "CallWall 110" in st.cell


def test_pinned_at_wall() -> None:
    # spot 109: 1pt under the 110 call wall → pinned, expect defense (fade down).
    st = GexMapTracker().update(build_profile(_chain(109.0, _ROWS)))
    assert st.position_state == "PINNED_AT_WALL"
    assert st.prior.expected_behavior == "WALL_DEFENSE"
    assert st.prior.favored_direction is Direction.BEARISH


def test_overshot_envelope() -> None:
    # spot 112: above the 110 call wall (top of the envelope) → overshoot. Positive
    # gamma → fade the extension back inside (BEARISH).
    st = GexMapTracker().update(build_profile(_chain(112.0, _ROWS)))
    assert st.position_state == "OVERSHOT_ENVELOPE"
    assert st.prior.expected_behavior == "FADE_EXTENSION"
    assert st.prior.favored_direction is Direction.BEARISH
    assert "overshot" in st.cell


# Wide-wall chain with the zero-Γ flip in open space (≈93.3), put wall 80, call walls
# 100/120 — used for the approaching-flip read. (All OI ≥ min_oi so nothing is filtered.)
_FLIP_ROWS = [(80.0, 10, 30), (100.0, 40, 10), (120.0, 60, 10)]


def test_approaching_flip() -> None:
    # Heading up from 86 → 88 toward the ≈93.3 flip (≈5pt away — beyond the 3pt pin,
    # within the 7pt approach), with no wall within 3pt → APPROACHING_FLIP.
    tracker = GexMapTracker()
    tracker.update(build_profile(_chain(86.0, _FLIP_ROWS)))    # seed heading
    st = tracker.update(build_profile(_chain(88.0, _FLIP_ROWS)))
    assert st.zero_gamma is not None and 92.0 < st.zero_gamma < 95.0
    assert st.position_state == "APPROACHING_FLIP"
    assert st.prior.expected_behavior == "FLIP_ACCELERATION"
    assert st.prior.favored_direction is Direction.BULLISH


# Clean negative-gamma chain (structural + net agree): call wall 80, put wall 120,
# zero-Γ ≈ 106.7, total -15k. Below the flip in open space → trend continuation.
_NEG_ROWS = [(80.0, 40, 10), (120.0, 10, 55)]


def test_negative_gamma_open_space_trend() -> None:
    tracker = GexMapTracker()
    tracker.update(build_profile(_chain(101.0, _NEG_ROWS)))
    st = tracker.update(build_profile(_chain(99.0, _NEG_ROWS)))    # heading BEARISH
    assert st.regime == "NEGATIVE_GAMMA"
    assert st.position_state == "IN_OPEN_SPACE"
    assert st.prior.expected_behavior == "TREND_CONTINUATION"
    assert st.prior.favored_direction is Direction.BEARISH


def test_wall_persistence_increments_and_resets() -> None:
    tracker = GexMapTracker()
    tracker.update(build_profile(_chain(104.0, _ROWS)))
    tracker.update(build_profile(_chain(104.0, _ROWS)))
    st = tracker.update(build_profile(_chain(104.0, _ROWS)))
    # The 110 call wall has been present three polls running.
    assert st.nearest_wall_above[0] == 110.0
    assert st.nearest_wall_above[2] == 3
    # Move the put wall 90 → 91: the old strike drops, the fresh one is provisional.
    moved = [(91.0, 10, 30), (110.0, 50, 10)]
    st2 = tracker.update(build_profile(_chain(104.0, moved)))
    assert st2.nearest_wall_above[2] == 4          # 110 kept persisting
    assert st2.nearest_wall_below[0] == 91.0
    assert st2.nearest_wall_below[2] == 1          # fresh wall, provisional


# --------------------------------------------------------------------------- #
# Stage 2 — the directional prior + assess()
# --------------------------------------------------------------------------- #

def test_prior_is_symmetric_with_location() -> None:
    # Nearer the call wall (spot 104) → fade DOWN; nearer the put wall (spot 96) →
    # fade UP. The lean flips with location — no baked-in directional bias.
    near_call = GexMapTracker().update(build_profile(_chain(104.0, _ROWS)))
    near_put = GexMapTracker().update(build_profile(_chain(96.0, _ROWS)))
    assert near_call.prior.expected_behavior == "MEAN_REVERT"
    assert near_call.prior.favored_direction is Direction.BEARISH
    assert near_put.prior.favored_direction is Direction.BULLISH


def test_assess_mean_revert_agree_and_against() -> None:
    st = GexMapTracker().update(build_profile(_chain(104.0, _ROWS)))  # fade down favored
    prior = st.prior
    # Agreeing fade with ample room → PROMOTE.
    assert prior.assess(Direction.BEARISH, room_pts=None).verdict == "PROMOTE"
    # A bullish continuation INTO a 4pt-away call wall (default 8pt floor) → OFF.
    off = prior.assess(Direction.BULLISH, room_pts=4.0)
    assert off.verdict == "OFF" and off.agrees is False
    # Same chase, but a momentum edge (4pt floor) with 4pt room → tolerated (OK).
    ok = prior.assess(Direction.BULLISH, is_momentum=True, room_pts=4.0)
    assert ok.verdict == "OK"


def test_assess_wall_defense_and_absorption() -> None:
    st = GexMapTracker().update(build_profile(_chain(109.0, _ROWS)))  # pinned at call wall
    prior = st.prior
    # Fading the defended wall (down) agrees.
    assert prior.assess(Direction.BEARISH).agrees is True
    # Breaking up THROUGH the defended wall → OFF.
    assert prior.assess(Direction.BULLISH).verdict == "OFF"
    # …unless CD absorption shows the wall is being eaten — then the break agrees.
    absorbed = prior.assess(Direction.BULLISH, absorption=True)
    assert absorbed.verdict in ("PROMOTE", "OK") and absorbed.agrees is True


def test_assess_flip_acceleration_against_is_off() -> None:
    tracker = GexMapTracker()
    tracker.update(build_profile(_chain(86.0, _FLIP_ROWS)))
    st = tracker.update(build_profile(_chain(88.0, _FLIP_ROWS)))   # approaching flip, up
    prior = st.prior
    assert prior.assess(Direction.BULLISH, room_pts=None).verdict == "PROMOTE"
    assert prior.assess(Direction.BEARISH).verdict == "OFF"   # fading an accelerating flip


def test_assess_stacked_wall_exhaustion_off() -> None:
    # A strongly-held wall (persistence ≥ 3) in the entry direction with no room is the
    # graded stacked-wall / re-test-fatigue failure → OFF even for a momentum edge.
    tracker = GexMapTracker()
    for _ in range(3):
        tracker.update(build_profile(_chain(104.0, _ROWS)))
    st = tracker.update(build_profile(_chain(104.0, _ROWS)))
    assert st.nearest_wall_above[2] >= 3
    off = st.prior.assess(
        Direction.BULLISH, is_momentum=True, room_pts=1.0, stacked_same_side=True
    )
    assert off.verdict == "OFF"


def test_agreement_tag() -> None:
    st = GexMapTracker().update(build_profile(_chain(104.0, _ROWS)))  # favored BEARISH
    assert st.prior.agreement(Direction.BEARISH) == "AGREES"
    assert st.prior.agreement(Direction.BULLISH) == "AGAINST"


# --------------------------------------------------------------------------- #
# Stage 3 — GexMapState.gate() (the shared pre/post entry point: room + stacked
# pulled off the state, then prior.assess)
# --------------------------------------------------------------------------- #

def test_state_gate_room_and_agreement() -> None:
    # spot 104: call wall 110 is 6pt up, put wall 90 is 14pt down; favored fade is down.
    st = GexMapTracker().update(build_profile(_chain(104.0, _ROWS)))
    # An agreeing fade (down) has 14pt of room → PROMOTE.
    assert st.gate(Direction.BEARISH).verdict == "PROMOTE"
    # A bullish chase into the 6pt-away call wall (8pt default floor) → OFF.
    assert st.gate(Direction.BULLISH).verdict == "OFF"
    # The same chase as a momentum edge (4pt floor, 6pt room) → tolerated (OK).
    assert st.gate(Direction.BULLISH, is_momentum=True).verdict == "OK"


def test_state_stacked_wall_flag_keeps_full_room() -> None:
    # Two call walls clustered above (108, 112) → a stacked same-side cluster that holds
    # the full 8pt room even for a momentum edge → a bullish push into it is OFF.
    rows = [(90.0, 10, 40), (108.0, 40, 10), (112.0, 45, 10)]
    st = GexMapTracker().update(build_profile(_chain(104.0, rows)))
    assert st.stacked_above is True
    assert st.gate(Direction.BULLISH, is_momentum=True).verdict == "OFF"


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
