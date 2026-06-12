"""
Local GEX math engine — pure, vectorized Pandas. No network, no awaits.

Pipeline:
    chain JSON  ->  flatten_chain()   ->  long-form contract DataFrame
                ->  compute_gex()     ->  per-contract dealer gamma exposure
                ->  aggregate_gex()   ->  per-strike Net GEX
                ->  GexProfile        ->  walls + zero-gamma flip + clusters

Conventions (per spec):
    Call GEX =  OI * gamma * 100 * spot      (dealers long gamma vs calls)
    Put  GEX = -OI * gamma * 100 * spot      (dealers short gamma vs puts)

Gamma uses the native value supplied by the Schwab API — no local BSM solve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from helenus.config import CONFIG

CONTRACT_FIELDS = [
    "strikePrice",
    "expirationDate",
    "totalVolume",
    "openInterest",
    "gamma",
    "mark",
    "delta",
]


# --------------------------------------------------------------------------- #
# JSON -> DataFrame
# --------------------------------------------------------------------------- #

def _flatten_side(exp_map: dict[str, Any], side: str) -> pd.DataFrame:
    """
    Schwab nests contracts as {expDateStr: {strikeStr: [contract, ...]}}.
    json_normalize-style flattening into one row per contract.
    """
    rows: list[dict[str, Any]] = []
    for exp_key, strikes in (exp_map or {}).items():
        for _strike_key, contracts in strikes.items():
            for c in contracts:
                row = {f: c.get(f) for f in CONTRACT_FIELDS}
                row["side"] = side
                row["expKey"] = exp_key
                rows.append(row)
    return pd.DataFrame(rows)


def flatten_chain(payload: dict[str, Any]) -> tuple[pd.DataFrame, float]:
    """
    Returns (contracts_df, spot).

    Spot prefers the embedded underlying quote; falls back to the chain-level
    underlyingPrice field.
    """
    spot = float(
        (payload.get("underlying") or {}).get("mark")
        or payload.get("underlyingPrice")
        or 0.0
    )

    df = pd.concat(
        [
            _flatten_side(payload.get("callExpDateMap", {}), "call"),
            _flatten_side(payload.get("putExpDateMap", {}), "put"),
        ],
        ignore_index=True,
    )
    if df.empty:
        return df, spot

    # Schwab emits NaN/-999 sentinels on stale greeks; coerce then scrub.
    num_cols = ["strikePrice", "totalVolume", "openInterest", "gamma", "mark", "delta"]
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    df = df[df["gamma"].between(0.0, 1.0)]          # kills -999 and junk
    df = df[df["openInterest"] >= CONFIG.gex.min_oi]
    return df.reset_index(drop=True), spot


# --------------------------------------------------------------------------- #
# Vectorized GEX
# --------------------------------------------------------------------------- #

def compute_gex(df: pd.DataFrame, spot: float) -> pd.DataFrame:
    """Adds a signed `gex` column (dollars of dealer gamma per 1-pt move)."""
    if df.empty:
        return df.assign(gex=pd.Series(dtype=float))
    sign = np.where(df["side"].eq("call"), 1.0, -1.0)
    df = df.assign(
        gex=sign
        * df["openInterest"]
        * df["gamma"]
        * CONFIG.gex.contract_multiplier
        * spot
    )
    return df


def aggregate_gex(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-strike rollup, sorted by strike ascending:
        call_gex, put_gex, net_gex, volume, oi
    """
    if df.empty:
        return pd.DataFrame(
            columns=["call_gex", "put_gex", "net_gex", "volume", "oi"]
        )
    pivot = df.pivot_table(
        index="strikePrice",
        columns="side",
        values="gex",
        aggfunc="sum",
        fill_value=0.0,
    )
    out = pd.DataFrame(
        {
            "call_gex": pivot.get("call", 0.0),
            "put_gex": pivot.get("put", 0.0),
        }
    )
    out["net_gex"] = out["call_gex"] + out["put_gex"]
    out["volume"] = df.groupby("strikePrice")["totalVolume"].sum()
    out["oi"] = df.groupby("strikePrice")["openInterest"].sum()
    return out.sort_index()


# --------------------------------------------------------------------------- #
# Structure extraction
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GexProfile:
    spot: float
    total_net_gex: float
    zero_gamma: float | None          # flip point (interpolated strike)
    call_walls: list[tuple[float, float]]   # [(strike, net_gex)] desc by magnitude
    put_walls: list[tuple[float, float]]
    by_strike: pd.DataFrame

    @property
    def regime(self) -> str:
        """Above flip in positive gamma -> mean reversion; below -> expansion."""
        if self.zero_gamma is None:
            return "UNKNOWN"
        return "POSITIVE_GAMMA" if self.spot >= self.zero_gamma else "NEGATIVE_GAMMA"

    def nearest_cluster_distance(self, price: float) -> float:
        """Distance in points from price to the nearest major GEX wall."""
        strikes = [s for s, _ in self.call_walls + self.put_walls]
        if not strikes:
            return float("inf")
        return min(abs(price - s) for s in strikes)


def _zero_gamma_flip(by_strike: pd.DataFrame) -> float | None:
    """
    Zero-gamma = strike where cumulative net GEX (low -> high) crosses zero.
    Linear interpolation between the two bracketing strikes.
    """
    if by_strike.empty:
        return None
    cum = by_strike["net_gex"].cumsum()
    signs = np.sign(cum.to_numpy())
    flips = np.where(np.diff(signs) != 0)[0]
    if len(flips) == 0:
        return None
    i = int(flips[0])
    k0, k1 = by_strike.index[i], by_strike.index[i + 1]
    c0, c1 = cum.iloc[i], cum.iloc[i + 1]
    if c1 == c0:
        return float(k0)
    return float(k0 + (k1 - k0) * (-c0) / (c1 - c0))


def build_profile(payload: dict[str, Any]) -> GexProfile:
    """Full pipeline: raw chain JSON -> GexProfile. Pure & synchronous."""
    contracts, spot = flatten_chain(payload)
    contracts = compute_gex(contracts, spot)
    by_strike = aggregate_gex(contracts)

    n = CONFIG.gex.wall_top_n
    pos = by_strike[by_strike["net_gex"] > 0]["net_gex"].nlargest(n)
    neg = by_strike[by_strike["net_gex"] < 0]["net_gex"].nsmallest(n)

    return GexProfile(
        spot=spot,
        total_net_gex=float(by_strike["net_gex"].sum()) if not by_strike.empty else 0.0,
        zero_gamma=_zero_gamma_flip(by_strike),
        call_walls=[(float(k), float(v)) for k, v in pos.items()],
        put_walls=[(float(k), float(v)) for k, v in neg.items()],
        by_strike=by_strike,
    )
