"""
Offline tests for engine/gex.py — the dealer-gamma math.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_gex.py
    python tests/test_gex.py

The fixture is hand-built so every Net GEX, wall, and the zero-gamma flip can be
computed by hand. With spot=100 and contract_multiplier=100, each contract's GEX
is  sign * OI * gamma * 100 * 100 = sign * (OI*gamma) * 10_000.
"""

from __future__ import annotations

import math

from helenus.engine.gex import build_profile, flatten_chain


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #

def _contract(strike: float, oi: float, gamma: float, vol: float) -> dict:
    return {
        "strikePrice": strike,
        "openInterest": oi,
        "gamma": gamma,
        "totalVolume": vol,
    }


def _chain(spot: float, calls: list[dict], puts: list[dict]) -> dict:
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


# Net GEX by strike (units of OI*gamma, then *10_000):
#   90:  call 10*0.1=1.0  put 40*0.1=4.0  ->  net_unit -3.0  -> -30_000
#  100:  call 60*0.1=6.0  put 10*0.1=1.0  ->  net_unit +5.0  -> +50_000
#  110:  call 30*0.1=3.0  put 10*0.1=1.0  ->  net_unit +2.0  -> +20_000
# Plus two contracts that must be filtered out:
#   call @105 gamma=-999 (stale sentinel)   -> dropped by gamma.between(0,1)
#   put  @95  openInterest=5 (< min_oi 10)  -> dropped by OI filter
_PAYLOAD = _chain(
    spot=100.0,
    calls=[
        _contract(90.0, 10, 0.10, 10),
        _contract(100.0, 60, 0.10, 200),
        _contract(110.0, 30, 0.10, 50),
        _contract(105.0, 100, -999.0, 5),   # junk gamma
    ],
    puts=[
        _contract(90.0, 40, 0.10, 20),
        _contract(100.0, 10, 0.10, 100),
        _contract(110.0, 10, 0.10, 30),
        _contract(95.0, 5, 0.10, 5),         # junk OI
    ],
)


# --------------------------------------------------------------------------- #
# Flatten + filtering
# --------------------------------------------------------------------------- #

def test_spot_prefers_underlying_mark() -> None:
    _df, spot = flatten_chain(_PAYLOAD)
    assert _approx(spot, 100.0)


def test_spot_falls_back_to_underlying_price() -> None:
    payload = {"underlyingPrice": 100.0, "callExpDateMap": {}, "putExpDateMap": {}}
    _df, spot = flatten_chain(payload)
    assert _approx(spot, 100.0)


def test_stale_gamma_and_low_oi_are_dropped() -> None:
    df, _spot = flatten_chain(_PAYLOAD)
    strikes = set(df["strikePrice"].tolist())
    assert 105.0 not in strikes          # gamma=-999 scrubbed
    assert 95.0 not in strikes           # openInterest < min_oi scrubbed
    assert strikes == {90.0, 100.0, 110.0}


# --------------------------------------------------------------------------- #
# GEX aggregation + sign convention
# --------------------------------------------------------------------------- #

def test_net_gex_sign_and_magnitude() -> None:
    p = build_profile(_PAYLOAD)
    bs = p.by_strike
    # Calls add positive gamma, puts subtract.
    assert _approx(float(bs.loc[90.0, "net_gex"]), -30_000)
    assert _approx(float(bs.loc[100.0, "net_gex"]), 50_000)
    assert _approx(float(bs.loc[110.0, "net_gex"]), 20_000)
    assert _approx(float(bs.loc[90.0, "call_gex"]), 10_000)
    assert _approx(float(bs.loc[90.0, "put_gex"]), -40_000)
    assert _approx(p.total_net_gex, 40_000)


def test_volume_and_oi_rollup() -> None:
    p = build_profile(_PAYLOAD)
    bs = p.by_strike
    assert _approx(float(bs.loc[100.0, "volume"]), 300)   # 200 call + 100 put
    assert _approx(float(bs.loc[100.0, "oi"]), 70)        # 60 call + 10 put


# --------------------------------------------------------------------------- #
# Structure: walls, zero-gamma, regime
# --------------------------------------------------------------------------- #

def test_walls() -> None:
    p = build_profile(_PAYLOAD)
    # Positive-net strikes, largest magnitude first.
    assert [(round(k), round(v)) for k, v in p.call_walls] == [(100, 50_000), (110, 20_000)]
    # Negative-net strikes, most negative first.
    assert [(round(k), round(v)) for k, v in p.put_walls] == [(90, -30_000)]


def test_zero_gamma_interpolation() -> None:
    # cumulative net GEX low->high: -30k, +20k, +40k. Flip is between 90 and 100.
    #   90 + (100-90) * 30_000 / (20_000 - (-30_000)) = 90 + 10*0.6 = 96.0
    p = build_profile(_PAYLOAD)
    assert p.zero_gamma is not None
    assert _approx(p.zero_gamma, 96.0)


def test_regime_and_cluster_distance() -> None:
    p = build_profile(_PAYLOAD)
    # spot 100 sits above the 96 flip AND total net GEX is positive (+40k) ->
    # both reads agree, clean positive-gamma (mean-reverting) regime.
    assert p.regime == "POSITIVE_GAMMA"
    # A wall sits exactly at spot (the 100 strike), so nearest distance is 0.
    assert _approx(p.nearest_cluster_distance(100.0), 0.0)
    assert _approx(p.nearest_cluster_distance(103.0), 3.0)


# Spot is ABOVE the first cumulative-zero flip (structural read = positive) but
# total net GEX is negative — the contradiction the engine used to mislabel as a
# clean POSITIVE_GAMMA. Net GEX by strike (units of OI*gamma * 10_000):
#   90:  call 10*0.1 - put 30*0.1 = -2.0 -> -20_000   (cum -20_000)
#  100:  call 35*0.1 - put 10*0.1 = +2.5 -> +25_000   (cum  +5_000 -> flip 90..100)
#  110:  call 10*0.1 - put 40*0.1 = -3.0 -> -30_000   (total -25_000)
_CONFLICT_PAYLOAD = _chain(
    spot=100.0,
    calls=[
        _contract(90.0, 10, 0.10, 10),
        _contract(100.0, 35, 0.10, 200),
        _contract(110.0, 10, 0.10, 50),
    ],
    puts=[
        _contract(90.0, 30, 0.10, 20),
        _contract(100.0, 10, 0.10, 100),
        _contract(110.0, 40, 0.10, 30),
    ],
)


def test_regime_conflict_trusts_net_gex_sign() -> None:
    p = build_profile(_CONFLICT_PAYLOAD)
    # Flip sits below spot (structural read says positive) but total net GEX is
    # negative -> resolve toward amplification and flag the conflict.
    assert p.zero_gamma is not None and p.zero_gamma < p.spot
    assert p.total_net_gex < 0
    assert p.regime == "NEGATIVE_GAMMA_CONFLICTED"


# --------------------------------------------------------------------------- #
# Empty / degenerate
# --------------------------------------------------------------------------- #

def test_empty_payload() -> None:
    p = build_profile({})
    assert p.by_strike.empty
    assert p.total_net_gex == 0.0
    assert p.zero_gamma is None
    assert p.call_walls == []
    assert p.put_walls == []
    assert p.regime == "UNKNOWN"
    assert p.nearest_cluster_distance(100.0) == float("inf")


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
