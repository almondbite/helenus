"""
Offline tests for engine/charm.py — the OTM-wing delta-decay (charm) engine.

No network, no Schwab key needed. Runs under pytest *or* standalone:

    pytest tests/test_charm.py          # if pytest is installed
    python tests/test_charm.py          # plain-assert fallback runner

Charm's BSM value is transcendental, so these assert the *structural* contract
the trade logic relies on — the signs, the OI scaling, the OTM-only restriction,
the time-of-day intensity — rather than hand-computed magnitudes.
"""

from __future__ import annotations

import datetime as dt
import math

from helenus.engine.charm import build_charm_profile, minutes_to_expiry

# America/New_York, to match the engine's settlement clock.
from helenus.data.schwab_feed import ET


# --------------------------------------------------------------------------- #
# Fixtures — canned Schwab-shaped chain payloads (strike, OI, IV%)
# --------------------------------------------------------------------------- #

def _chain(
    spot: float,
    calls: list[tuple[float, float, float]],
    puts: list[tuple[float, float, float]],
) -> dict:
    """Build a minimal $SPXW-shaped chain. Rows are (strike, openInterest, IV%)."""

    def side_map(rows: list[tuple[float, float, float]]) -> dict:
        strikes: dict[str, list[dict]] = {}
        for strike, oi, iv in rows:
            strikes[f"{strike:.1f}"] = [
                {"strikePrice": strike, "openInterest": oi, "volatility": iv}
            ]
        return {"2026-06-19:0": strikes}

    return {
        "underlying": {"mark": spot},
        "callExpDateMap": side_map(calls),
        "putExpDateMap": side_map(puts),
    }


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


_SPOT = 5000.0
_MTE = 120.0  # 2h to the bell — the afternoon window

# Balanced wings: equal-ish OI of OTM calls (above) and OTM puts (below).
_BALANCED = _chain(
    _SPOT,
    calls=[(5010.0, 4000, 15.0), (5020.0, 3000, 15.0)],
    puts=[(4990.0, 4000, 15.0), (4980.0, 3000, 15.0)],
)


# --------------------------------------------------------------------------- #
# Sign / bias
# --------------------------------------------------------------------------- #

def test_otm_puts_make_positive_supportive_charm() -> None:
    # Put-heavy OTM book: puts decay -> dealers buy futures back -> support.
    payload = _chain(
        _SPOT,
        calls=[(5010.0, 500, 15.0)],
        puts=[(4990.0, 8000, 15.0), (4980.0, 6000, 15.0)],
    )
    p = build_charm_profile(payload, _MTE)
    assert p.put_support > 0
    assert p.net_charm > 0                       # supportive overall
    assert p.bias == "SUPPORTIVE"
    assert "floor" in p.drift


def test_otm_calls_make_negative_overhead_charm() -> None:
    # Call-heavy OTM book: calls decay -> dealers sell futures -> overhead.
    payload = _chain(
        _SPOT,
        calls=[(5010.0, 8000, 15.0), (5020.0, 6000, 15.0)],
        puts=[(4990.0, 500, 15.0)],
    )
    p = build_charm_profile(payload, _MTE)
    assert p.call_overhead > 0
    assert p.net_charm < 0                        # overhead overall
    assert p.bias == "OVERHEAD"


def test_balanced_wings_are_balanced() -> None:
    p = build_charm_profile(_BALANCED, _MTE)
    # net ≈ 0 and neither wing clears the dominance ratio.
    assert abs(p.net_charm) < p.put_support       # the two wings largely cancel
    assert p.bias == "BALANCED"


# --------------------------------------------------------------------------- #
# OI scaling and OTM-only restriction
# --------------------------------------------------------------------------- #

def test_charm_scales_with_open_interest() -> None:
    small = build_charm_profile(
        _chain(_SPOT, calls=[(5010.0, 1000, 15.0)], puts=[]), _MTE
    )
    big = build_charm_profile(
        _chain(_SPOT, calls=[(5010.0, 5000, 15.0)], puts=[]), _MTE
    )
    # 5x the OI -> 5x the charm magnitude (linear in OI).
    assert _approx(big.call_overhead, 5.0 * small.call_overhead, tol=1e-6)


def test_itm_contracts_are_ignored() -> None:
    # Only OTM wings carry the structural story: an ITM call (strike<spot) and an
    # ITM put (strike>spot) must contribute nothing.
    payload = _chain(
        _SPOT,
        calls=[(4980.0, 9000, 15.0)],   # ITM call
        puts=[(5020.0, 9000, 15.0)],    # ITM put
    )
    p = build_charm_profile(payload, _MTE)
    assert p.net_charm == 0.0
    assert p.put_support == 0.0
    assert p.call_overhead == 0.0
    assert p.support_walls == []
    assert p.resistance_walls == []


def test_walls_are_populated_and_signed() -> None:
    p = build_charm_profile(_BALANCED, _MTE)
    assert len(p.support_walls) == 2
    assert len(p.resistance_walls) == 2
    # Support walls (OTM puts) carry positive charm; resistance (OTM calls) negative.
    assert all(v > 0 for _, v in p.support_walls)
    assert all(v < 0 for _, v in p.resistance_walls)


# --------------------------------------------------------------------------- #
# Time-of-day intensity (charm ∝ 1/T)
# --------------------------------------------------------------------------- #

def test_intensity_ramps_into_the_afternoon() -> None:
    morning = build_charm_profile(_BALANCED, 330.0)   # ~10:30, >4h left
    midday = build_charm_profile(_BALANCED, 200.0)    # ~12:40
    afternoon = build_charm_profile(_BALANCED, 90.0)  # ~14:30
    assert morning.intensity == "LOW"
    assert midday.intensity == "BUILDING"
    assert afternoon.intensity == "HIGH"


def test_charm_magnitude_grows_as_expiry_nears() -> None:
    # Same book, less time left -> larger per-contract charm (1/T), so a bigger
    # net wing — the mechanical reason the melt-up is an afternoon event.
    payload = _chain(_SPOT, calls=[(5010.0, 5000, 15.0)], puts=[])
    early = build_charm_profile(payload, 300.0)
    late = build_charm_profile(payload, 60.0)
    assert late.call_overhead > early.call_overhead


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def test_empty_payload_is_safe() -> None:
    p = build_charm_profile({}, _MTE)
    assert p.net_charm == 0.0
    assert p.support_walls == []
    assert p.by_strike.empty


def test_after_the_bell_is_empty() -> None:
    # mins_to_expiry <= 0 -> no charm (and no div-by-zero).
    p = build_charm_profile(_BALANCED, 0.0)
    assert p.net_charm == 0.0
    assert p.intensity == "EXPIRED"


def test_junk_iv_rows_are_dropped() -> None:
    # Schwab's -999 IV sentinel must not poison the math.
    payload = _chain(
        _SPOT,
        calls=[(5010.0, 5000, -999.0)],   # stale greek
        puts=[(4990.0, 5000, 15.0)],
    )
    p = build_charm_profile(payload, _MTE)
    assert p.call_overhead == 0.0          # the junk call row was dropped
    assert p.put_support > 0


# --------------------------------------------------------------------------- #
# minutes_to_expiry helper
# --------------------------------------------------------------------------- #

def test_minutes_to_expiry_counts_down_to_settlement() -> None:
    at_two = dt.datetime(2026, 6, 22, 14, 0, tzinfo=ET)
    assert _approx(minutes_to_expiry(at_two), 120.0)


def test_minutes_to_expiry_clamps_after_close() -> None:
    after = dt.datetime(2026, 6, 22, 17, 0, tzinfo=ET)
    assert minutes_to_expiry(after) == 0.0


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
