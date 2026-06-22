"""
Options-volume flow — the edge layer.

Two things live here, both 0DTE-only (the chain is already pinned to today):

  1. VolumeProfile — where today's option volume sits relative to spot, split
     ITM/OTM for calls and puts. This is the volume *distribution*: a snapshot.

  2. VannaTracker / VannaReading — the *flow*: how that distribution is changing
     between chain polls, read against the VIX trend. A "vanna rally" is the
     setup where VIX is falling (calls get cheaper) and fresh OTM call volume is
     outpacing OTM put volume — dealers short those calls hedge by buying the
     underlying, which lifts spot. It's most potent as a reversal when price is
     weak: the tape is falling, then call flow + a VIX roll-over turns it.

Convention (relative to spot):
    call ITM: strike <= spot      call OTM: strike >  spot
    put  ITM: strike >= spot      put  OTM: strike <  spot

Pure and deterministic — no I/O, no awaits. Volume math only; this is Claude's
input, not its job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from helenus.config import CONFIG


# --------------------------------------------------------------------------- #
# JSON -> per-strike call/put volume
# --------------------------------------------------------------------------- #

def _flatten_volume(exp_map: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """One row per contract: {strike, volume, side}. No OI/greek filtering —
    we want raw traded volume, including fresh strikes with little open interest."""
    rows: list[dict[str, Any]] = []
    for _exp_key, strikes in (exp_map or {}).items():
        for _strike_key, contracts in strikes.items():
            for c in contracts:
                rows.append(
                    {
                        "strike": c.get("strikePrice"),
                        "volume": c.get("totalVolume"),
                        "side": side,
                    }
                )
    return rows


# --------------------------------------------------------------------------- #
# Volume profile
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VolumeProfile:
    spot: float
    by_strike: pd.DataFrame          # index=strike, cols: call_volume, put_volume, total
    otm_call_vol: float
    itm_call_vol: float
    otm_put_vol: float
    itm_put_vol: float

    @property
    def total_call_vol(self) -> float:
        return self.otm_call_vol + self.itm_call_vol

    @property
    def total_put_vol(self) -> float:
        return self.otm_put_vol + self.itm_put_vol

    @property
    def call_put_ratio(self) -> float:
        return self.total_call_vol / max(self.total_put_vol, 1.0)

    @property
    def otm_call_put_ratio(self) -> float:
        """OTM call volume vs OTM put volume — the directional-speculation tilt."""
        return self.otm_call_vol / max(self.otm_put_vol, 1.0)

    @property
    def above_spot_vol(self) -> float:
        if self.by_strike.empty:
            return 0.0
        return float(self.by_strike[self.by_strike.index > self.spot]["total"].sum())

    @property
    def below_spot_vol(self) -> float:
        if self.by_strike.empty:
            return 0.0
        return float(self.by_strike[self.by_strike.index < self.spot]["total"].sum())


def build_volume_profile(payload: dict[str, Any]) -> VolumeProfile:
    """Raw $SPXW 0DTE chain JSON -> VolumeProfile. Pure & synchronous."""
    spot = float(
        (payload.get("underlying") or {}).get("mark")
        or payload.get("underlyingPrice")
        or 0.0
    )
    rows = _flatten_volume(payload.get("callExpDateMap", {}), "call")
    rows += _flatten_volume(payload.get("putExpDateMap", {}), "put")
    df = pd.DataFrame(rows)

    empty = pd.DataFrame(columns=["call_volume", "put_volume", "total"])
    if df.empty:
        return VolumeProfile(spot, empty, 0.0, 0.0, 0.0, 0.0)

    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["strike"])
    df = df[df["volume"] >= 0]
    if df.empty:
        return VolumeProfile(spot, empty, 0.0, 0.0, 0.0, 0.0)

    calls = df[df["side"] == "call"]
    puts = df[df["side"] == "put"]
    itm_call = float(calls[calls["strike"] <= spot]["volume"].sum())
    otm_call = float(calls[calls["strike"] > spot]["volume"].sum())
    itm_put = float(puts[puts["strike"] >= spot]["volume"].sum())
    otm_put = float(puts[puts["strike"] < spot]["volume"].sum())

    pivot = df.pivot_table(
        index="strike", columns="side", values="volume", aggfunc="sum", fill_value=0.0
    )
    by_strike = pd.DataFrame(
        {
            "call_volume": pivot.get("call", 0.0),
            "put_volume": pivot.get("put", 0.0),
        }
    )
    by_strike["total"] = by_strike["call_volume"] + by_strike["put_volume"]
    by_strike = by_strike.sort_index()

    return VolumeProfile(
        spot=spot,
        by_strike=by_strike,
        otm_call_vol=otm_call,
        itm_call_vol=itm_call,
        otm_put_vol=otm_put,
        itm_put_vol=itm_put,
    )


# --------------------------------------------------------------------------- #
# Vanna tracker
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VannaReading:
    vix_change: float                # over the configured lookback (negative = falling)
    vix_falling: bool
    otm_call_flow: float             # fresh OTM call volume this interval
    otm_put_flow: float              # fresh OTM put volume this interval
    call_flow_dominance: float       # otm_call_flow - otm_put_flow
    active: bool                     # the bullish vanna-rally setup is live
    bearish_active: bool             # the bearish analogue (put-flow pressure) is live
    label: str
    note: str


class VannaTracker:
    """
    Stateful: holds the previous VolumeProfile so it can turn cumulative daily
    volume into per-interval *flow*, and reads that flow against the VIX trend.
    Updated once per chain poll.
    """

    def __init__(self) -> None:
        self._prev: VolumeProfile | None = None

    def update(self, profile: VolumeProfile, vix_history: list[float]) -> VannaReading:
        cfg = CONFIG.flow

        # Flow = interval delta of cumulative volume. clamp at 0 so the daily
        # reset (today's cumulative < yesterday's) can't fake a surge.
        if self._prev is not None:
            otm_call_flow = max(0.0, profile.otm_call_vol - self._prev.otm_call_vol)
            otm_put_flow = max(0.0, profile.otm_put_vol - self._prev.otm_put_vol)
        else:
            otm_call_flow = otm_put_flow = 0.0
        self._prev = profile

        # VIX trend over the lookback window.
        vix_change = 0.0
        if len(vix_history) >= 2:
            k = min(len(vix_history) - 1, cfg.vix_lookback_samples)
            vix_change = float(vix_history[-1] - vix_history[-1 - k])
        vix_falling = vix_change <= -cfg.vix_drop_pts
        vix_rising = vix_change >= cfg.vix_drop_pts

        dominance = otm_call_flow - otm_put_flow
        active = (
            vix_falling
            and otm_call_flow >= cfg.min_call_flow
            and otm_call_flow >= cfg.call_dominance_ratio * max(otm_put_flow, 1.0)
        )
        # Bearish analogue: VIX rising makes puts the demand, buyers pile into OTM
        # puts, dealers short those puts hedge by SELLING the underlying — vanna
        # pressure to the downside. Symmetric thresholds to the rally.
        bearish_active = (
            vix_rising
            and otm_put_flow >= cfg.min_call_flow
            and otm_put_flow >= cfg.call_dominance_ratio * max(otm_call_flow, 1.0)
        )

        if active:
            label = "VANNA RALLY BUILDING"
            note = (
                f"VIX {vix_change:+.2f} (falling) with fresh OTM call flow "
                f"{otm_call_flow:,.0f} vs put {otm_put_flow:,.0f} "
                f"({otm_call_flow / max(otm_put_flow, 1.0):.1f}x) — cheaper calls "
                "drawing buyers; dealer hedging supports spot."
            )
        elif bearish_active:
            label = "PUT FLOW PRESSURE"
            note = (
                f"VIX {vix_change:+.2f} (rising) with fresh OTM put flow "
                f"{otm_put_flow:,.0f} vs call {otm_call_flow:,.0f} "
                f"({otm_put_flow / max(otm_call_flow, 1.0):.1f}x) — put demand; "
                "dealer hedging pressures spot lower."
            )
        elif vix_falling and dominance > 0:
            label = "vanna leaning bullish"
            note = (
                f"VIX {vix_change:+.2f}, OTM call flow leading puts "
                f"({otm_call_flow:,.0f} vs {otm_put_flow:,.0f}) but below trigger."
            )
        elif vix_rising and dominance < 0:
            label = "leaning bearish"
            note = (
                f"VIX {vix_change:+.2f}, OTM put flow leading calls "
                f"({otm_put_flow:,.0f} vs {otm_call_flow:,.0f}) but below trigger."
            )
        else:
            label = "no flow setup"
            note = f"VIX {vix_change:+.2f}; OTM call flow {otm_call_flow:,.0f} vs put {otm_put_flow:,.0f}."

        return VannaReading(
            vix_change=round(vix_change, 2),
            vix_falling=vix_falling,
            otm_call_flow=otm_call_flow,
            otm_put_flow=otm_put_flow,
            call_flow_dominance=dominance,
            active=active,
            bearish_active=bearish_active,
            label=label,
            note=note,
        )
