"""
Local charm (delta-decay) engine — pure, vectorized Pandas. No network, no awaits.

Charm is ∂Δ/∂t: the rate at which an option's delta bleeds away as time passes.
For 0DTE it is one of the dominant intraday structural forces, and it tells a
specific dealer-flow story:

  The mechanic. Customers are net long the OTM 0DTE wings (lottery puts/calls).
  Dealers are short them and hedge in the underlying. As time passes the wings
  lose delta (charm), so the dealer's hedge becomes too large and must be
  unwound — mechanical buying or selling of futures that has nothing to do with
  conviction:

    * OTM PUTS  -> dealers were short futures to hedge; as put delta decays they
      BUY futures back  ->  POSITIVE charm, SUPPORTIVE (a floor under price; the
      classic low-volume "afternoon melt-up" when the morning crash never came).
    * OTM CALLS -> dealers were long futures to hedge; as call delta decays they
      SELL futures      ->  NEGATIVE charm, OVERHEAD (a weight over price).

  Two amplifiers:
    * Open interest. The more OI sitting in a wing, the more delta there is to
      decay, so the more futures must be unwound. Charm magnitude scales with OI.
    * Time. Charm ∝ 1/T, so the effect is small in the morning and accelerates
      hard into the afternoon — which is why the melt-up is an afternoon
      phenomenon ("if it hasn't crashed by 2 PM...").

Convention (matches GEX's sign discipline):
    net_charm > 0  ->  SUPPORTIVE  (OTM-put support dominates) — bullish drift
    net_charm < 0  ->  OVERHEAD    (OTM-call weight dominates)  — bearish drift

Pipeline:
    chain JSON  ->  _flatten()            ->  OTM contract DataFrame (+ IV)
                ->  build_charm_profile()  ->  per-strike dealer charm $ + walls

Charm is computed analytically (Black-Scholes ∂Δ/∂t) from the strike, the
contract's own implied vol, and the live minutes-to-expiry — Schwab does not
emit charm natively the way it does gamma/delta.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from helenus.config import CONFIG

_MINUTES_PER_YEAR = 60.0 * 24.0 * 365.0


# --------------------------------------------------------------------------- #
# Time to expiry
# --------------------------------------------------------------------------- #

def minutes_to_expiry(now: dt.datetime) -> float:
    """Minutes from `now` to today's SPXW PM settlement (16:00 ET by default).

    Pure in `now` so the caller owns the clock and tests can pin it. Clamped at
    zero (never negative); the engine floors it further before dividing."""
    cfg = CONFIG.charm
    settle = now.replace(
        hour=cfg.settle_hour, minute=cfg.settle_minute, second=0, microsecond=0
    )
    return max(0.0, (settle - now).total_seconds() / 60.0)


# --------------------------------------------------------------------------- #
# JSON -> OTM contract DataFrame
# --------------------------------------------------------------------------- #

def _flatten(payload: dict[str, Any]) -> pd.DataFrame:
    """One row per contract: {strike, oi, iv, side}. IV is Schwab's per-contract
    `volatility` (annualized, in percent)."""
    rows: list[dict[str, Any]] = []
    for side, key in (("call", "callExpDateMap"), ("put", "putExpDateMap")):
        for _exp_key, strikes in (payload.get(key, {}) or {}).items():
            for _strike_key, contracts in strikes.items():
                for c in contracts:
                    rows.append(
                        {
                            "strike": c.get("strikePrice"),
                            "oi": c.get("openInterest"),
                            "iv": c.get("volatility"),
                            "side": side,
                        }
                    )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Black-Scholes charm (∂Δ/∂t)
# --------------------------------------------------------------------------- #

def _norm_pdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)


def _bsm_charm_per_day(
    spot: float, strikes: np.ndarray, iv: np.ndarray, t_years: float
) -> np.ndarray:
    """∂Δ/∂t per calendar day, with r = q = 0 (rates are negligible over hours).

    Under r = q = 0 the call and put charm coincide:  charm = φ(d1)·d2 / (2T).
    The sign falls out of moneyness: OTM calls (S<K) give d2<0 -> negative charm
    (delta bleeds toward 0); OTM puts (S>K) give d2>0 -> positive charm (a put's
    negative delta bleeds up toward 0). Returned per-year value is divided by 365.
    """
    srt = iv * np.sqrt(t_years)
    d1 = (np.log(spot / strikes) + 0.5 * iv * iv * t_years) / srt
    d2 = d1 - srt
    charm_per_year = _norm_pdf(d1) * d2 / (2.0 * t_years)
    return charm_per_year / 365.0


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CharmProfile:
    spot: float
    minutes_to_expiry: float
    net_charm: float                # signed $; + supportive (bull), − overhead (bear)
    put_support: float              # ≥ 0 — supportive charm from OTM puts
    call_overhead: float            # ≥ 0 — overhead charm from OTM calls
    support_walls: list[tuple[float, float]]     # [(strike, charm$)] OTM puts, desc
    resistance_walls: list[tuple[float, float]]  # [(strike, charm$)] OTM calls, desc mag
    by_strike: pd.DataFrame         # index=strike, cols: charm, oi, side

    @property
    def bias(self) -> str:
        """SUPPORTIVE / OVERHEAD / BALANCED — which wing's un-hedging dominates."""
        s, o = self.put_support, self.call_overhead
        ratio = CONFIG.charm.dominance_ratio
        if s >= ratio * max(o, 1.0):
            return "SUPPORTIVE"
        if o >= ratio * max(s, 1.0):
            return "OVERHEAD"
        return "BALANCED"

    @property
    def intensity(self) -> str:
        """How forceful charm is *now*. It scales with 1/T, so it builds through
        the day: LOW in the morning, BUILDING past midday, HIGH in the afternoon
        melt-up window (≤2h to the bell)."""
        m = self.minutes_to_expiry
        if m <= 0:
            return "EXPIRED"
        if m <= 120:
            return "HIGH"
        if m <= 240:
            return "BUILDING"
        return "LOW"

    @property
    def drift(self) -> str:
        """Plain-language read for the snapshot/embed."""
        if self.bias == "SUPPORTIVE":
            return "supportive — OTM-put charm pins a floor (afternoon-melt-up risk)"
        if self.bias == "OVERHEAD":
            return "overhead — OTM-call charm caps rallies (drift lower into close)"
        return "balanced — neither wing's charm dominates"


def _empty(spot: float, mte: float) -> CharmProfile:
    cols = pd.DataFrame({c: pd.Series(dtype=float) for c in ("charm", "oi", "side")})
    return CharmProfile(spot, max(mte, 0.0), 0.0, 0.0, 0.0, [], [], cols)


def build_charm_profile(
    payload: dict[str, Any], mins_to_expiry: float
) -> CharmProfile:
    """Full pipeline: raw 0DTE chain JSON + minutes-to-expiry -> CharmProfile.

    Pure & synchronous. `mins_to_expiry` is supplied by the caller (see
    `minutes_to_expiry`) so the math module stays clock-free and testable.
    """
    cfg = CONFIG.charm
    spot = float(
        (payload.get("underlying") or {}).get("mark")
        or payload.get("underlyingPrice")
        or 0.0
    )
    if spot <= 0 or mins_to_expiry <= 0:
        return _empty(spot, mins_to_expiry)

    df = _flatten(payload)
    if df.empty:
        return _empty(spot, mins_to_expiry)

    for col in ("strike", "oi", "iv"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Schwab emits -999 IV sentinels on stale greeks; keep only sane, liquid rows.
    df = df[df["iv"].between(0.01, 500.0)]          # percent units, drop junk
    df = df[df["oi"] >= cfg.min_oi]
    df = df.dropna(subset=["strike"])
    # Charm's structural story is the OTM wings only.
    otm = ((df["side"] == "call") & (df["strike"] > spot)) | (
        (df["side"] == "put") & (df["strike"] < spot)
    )
    df = df[otm]
    if df.empty:
        return _empty(spot, mins_to_expiry)

    t_years = max(mins_to_expiry, cfg.min_minutes_floor) / _MINUTES_PER_YEAR
    charm_day = _bsm_charm_per_day(
        spot,
        df["strike"].to_numpy(dtype=float),
        df["iv"].to_numpy(dtype=float) / 100.0,   # percent -> decimal
        t_years,
    )
    # Dealer charm in dollar-delta-per-day terms (OI · charm · 100 · spot),
    # mirroring the GEX convention. Sign already correct: puts +, calls −.
    df = df.assign(
        charm=charm_day * df["oi"].to_numpy(dtype=float) * cfg.contract_multiplier * spot
    )

    puts = df[df["side"] == "put"]
    calls = df[df["side"] == "call"]
    put_support = float(puts["charm"].sum())          # ≥ 0
    call_overhead = float(-calls["charm"].sum())      # ≥ 0
    net_charm = float(df["charm"].sum())

    n = cfg.wall_top_n
    support = puts.nlargest(n, "charm")[["strike", "charm"]]
    resist = calls.nsmallest(n, "charm")[["strike", "charm"]]   # most negative

    by_strike = (
        df.groupby("strike")
        .agg(charm=("charm", "sum"), oi=("oi", "sum"), side=("side", "first"))
        .sort_index()
    )

    return CharmProfile(
        spot=spot,
        minutes_to_expiry=mins_to_expiry,
        net_charm=net_charm,
        put_support=put_support,
        call_overhead=call_overhead,
        support_walls=[(float(r.strike), float(r.charm)) for r in support.itertuples()],
        resistance_walls=[(float(r.strike), float(r.charm)) for r in resist.itertuples()],
        by_strike=by_strike,
    )
