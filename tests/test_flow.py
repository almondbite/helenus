"""
Offline tests for engine/flow.py — options-volume buckets and the vanna tracker.

No network, no Schwab key needed. Runs under pytest *or* standalone:

    pytest tests/test_flow.py          # if pytest is installed
    python tests/test_flow.py          # plain-assert fallback runner
"""

from __future__ import annotations

import math

from helenus.engine.flow import VannaTracker, build_volume_profile


# --------------------------------------------------------------------------- #
# Fixtures — canned Schwab-shaped chain payloads
# --------------------------------------------------------------------------- #

def _chain(
    spot: float,
    calls: list[tuple[float, float]],
    puts: list[tuple[float, float]],
) -> dict:
    """Build a minimal $SPXW-shaped chain: {expDate: {strike: [contract]}}."""

    def side_map(rows: list[tuple[float, float]]) -> dict:
        strikes: dict[str, list[dict]] = {}
        for strike, vol in rows:
            strikes[f"{strike:.1f}"] = [
                {"strikePrice": strike, "totalVolume": vol}
            ]
        return {"2026-06-19:0": strikes}

    return {
        "underlying": {"mark": spot},
        "callExpDateMap": side_map(calls),
        "putExpDateMap": side_map(puts),
    }


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# --------------------------------------------------------------------------- #
# Volume buckets
# --------------------------------------------------------------------------- #

def test_itm_otm_buckets() -> None:
    # spot 5000. Calls: 4980 ITM, 5020/5040 OTM. Puts: 5020 ITM, 4980/4960 OTM.
    payload = _chain(
        spot=5000.0,
        calls=[(4980.0, 100), (5020.0, 300), (5040.0, 500)],
        puts=[(5020.0, 50), (4980.0, 200), (4960.0, 100)],
    )
    vp = build_volume_profile(payload)

    assert _approx(vp.spot, 5000.0)
    assert _approx(vp.itm_call_vol, 100)          # strike <= spot
    assert _approx(vp.otm_call_vol, 800)          # 300 + 500
    assert _approx(vp.itm_put_vol, 50)            # strike >= spot
    assert _approx(vp.otm_put_vol, 300)           # 200 + 100
    assert _approx(vp.total_call_vol, 900)
    assert _approx(vp.total_put_vol, 350)
    assert _approx(vp.call_put_ratio, 900 / 350)
    assert _approx(vp.otm_call_put_ratio, 800 / 300)


def test_above_below_spot() -> None:
    payload = _chain(
        spot=5000.0,
        calls=[(4980.0, 100), (5020.0, 300), (5040.0, 500)],
        puts=[(5020.0, 50), (4980.0, 200), (4960.0, 100)],
    )
    vp = build_volume_profile(payload)
    # Above: 5020 (300c+50p=350) + 5040 (500c) = 850
    assert _approx(vp.above_spot_vol, 850)
    # Below: 4980 (100c+200p=300) + 4960 (100p) = 400
    assert _approx(vp.below_spot_vol, 400)
    # Per-strike merge: 4980 carries both a call and a put leg.
    row = vp.by_strike.loc[4980.0]
    assert _approx(float(row["call_volume"]), 100)
    assert _approx(float(row["put_volume"]), 200)
    assert _approx(float(row["total"]), 300)


def test_empty_payload() -> None:
    vp = build_volume_profile({})
    assert vp.by_strike.empty
    assert vp.total_call_vol == 0.0
    assert vp.total_put_vol == 0.0
    assert vp.call_put_ratio == 0.0            # guarded against div-by-zero
    assert vp.above_spot_vol == 0.0


# --------------------------------------------------------------------------- #
# Vanna tracker
# --------------------------------------------------------------------------- #

# Prev poll: light OTM volume. Curr poll: a surge in OTM calls, puts flat-ish.
# Flow = curr - prev  ->  otm_call_flow 3000, otm_put_flow 300.
_PREV = _chain(spot=5000.0, calls=[(5040.0, 1000)], puts=[(4960.0, 500)])
_CURR = _chain(spot=5000.0, calls=[(5040.0, 4000)], puts=[(4960.0, 800)])

_VIX_FALLING = [15.0, 14.9, 14.8, 14.7, 14.6, 14.5, 14.0]   # last - last[-7] = -1.0
_VIX_RISING = [13.0, 13.1, 13.2, 13.3, 13.4, 13.5, 14.0]    # +1.0


def test_vanna_trips_on_falling_vix_and_call_flow() -> None:
    tracker = VannaTracker()
    first = tracker.update(build_volume_profile(_PREV), _VIX_FALLING)
    assert first.active is False                # no prior -> flow is 0

    reading = tracker.update(build_volume_profile(_CURR), _VIX_FALLING)
    assert _approx(reading.otm_call_flow, 3000)
    assert _approx(reading.otm_put_flow, 300)
    assert reading.vix_falling is True
    assert reading.active is True
    assert "VANNA RALLY" in reading.label.upper()


def test_vanna_quiet_when_vix_not_falling() -> None:
    tracker = VannaTracker()
    tracker.update(build_volume_profile(_PREV), _VIX_RISING)
    reading = tracker.update(build_volume_profile(_CURR), _VIX_RISING)
    assert _approx(reading.otm_call_flow, 3000)   # flow is there...
    assert reading.vix_falling is False           # ...but VIX isn't cooperating
    assert reading.active is False


def test_vanna_quiet_when_call_flow_too_small() -> None:
    # Curr barely above prev on calls -> flow below min_call_flow threshold.
    weak_curr = _chain(spot=5000.0, calls=[(5040.0, 1100)], puts=[(4960.0, 520)])
    tracker = VannaTracker()
    tracker.update(build_volume_profile(_PREV), _VIX_FALLING)
    reading = tracker.update(build_volume_profile(weak_curr), _VIX_FALLING)
    assert _approx(reading.otm_call_flow, 100)
    assert reading.active is False


def test_daily_reset_does_not_fake_a_surge() -> None:
    # New session: cumulative volume resets lower than yesterday's last poll.
    tracker = VannaTracker()
    tracker.update(build_volume_profile(_CURR), _VIX_FALLING)        # high cumulative
    reading = tracker.update(build_volume_profile(_PREV), _VIX_FALLING)  # lower (reset)
    assert reading.otm_call_flow == 0.0          # clamped, not negative
    assert reading.active is False


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
