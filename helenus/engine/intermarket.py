"""
Intermarket convergence — does the broader equity complex confirm an SPX signal?

Helenus judges $SPX structure in isolation; this layer corroborates a setup
against the rest of the tape so the engine doesn't fire a bullish SPX break while
QQQ is rolling over or futures order-flow leans the other way (the classic
intermarket-divergence trap). Two reads, both pure and synchronous:

  1. ES (/ES) Level-1 microstructure — futures *volume flow* (interval delta of
     cumulative volume) and *resting bid/ask size* (order-book imbalance). The
     intent/participation tell the cash index can't print.

  2. SPY + QQQ 0DTE gamma structure — each gets its own GexProfile (regime,
     zero-Γ, net-GEX sign) plus an intraday directional lean from %-vs-prior-
     close. QQQ is the lead breadth tell (semi/tech heavy); SPY corroborates.

Scoring (consumed by the analyst): when QQQ's intraday direction AND its gamma
regime agree with the signal direction AND SPY also confirms, conviction gets a
bounded mechanical boost layered on Claude's verdict. QQQ opposing the signal is
surfaced as a divergence warning. All thresholds live in CONFIG.intermarket.

This module is Claude's *input* and a deterministic scorer — never network I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from helenus.config import CONFIG, IntermarketConfig
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Direction


# --------------------------------------------------------------------------- #
# Quote helpers
# --------------------------------------------------------------------------- #

def _quote(macro_quotes: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Pull the inner Level-1 `quote` block for a symbol out of a Schwab quote
    payload ({symbol: {"quote": {...}}}). Empty dict if absent."""
    return (macro_quotes.get(symbol, {}) or {}).get("quote") or {}


def _num(d: dict[str, Any], *keys: str) -> float:
    """First present, numeric value among `keys`, else 0.0. Schwab futures
    payloads vary in which field carries last/mark, so we try in order."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _regime_base(regime: str) -> str:
    """Collapse a GexProfile.regime label to its base token for agreement checks:
    'NEGATIVE_GAMMA_CONFLICTED' -> 'NEGATIVE', 'POSITIVE_GAMMA' -> 'POSITIVE',
    'UNKNOWN' -> 'UNKNOWN'. The _CONFLICTED suffix is already resolved toward the
    net-GEX truth upstream, so comparing base tokens is the right granularity."""
    if regime.startswith("POSITIVE"):
        return "POSITIVE"
    if regime.startswith("NEGATIVE"):
        return "NEGATIVE"
    return "UNKNOWN"


def _leg_direction(pct_change: float, deadband: float) -> Direction | None:
    """Intraday momentum lean from %-vs-prior-close, with a deadband so a flat
    tape reads as no-lean (None) rather than a coin-flip direction."""
    if pct_change >= deadband:
        return Direction.BULLISH
    if pct_change <= -deadband:
        return Direction.BEARISH
    return None


# --------------------------------------------------------------------------- #
# ES futures microstructure
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ESReading:
    last: float
    pct_change: float                # intraday % vs prior settle
    volume_flow: float               # fresh contracts this interval (cum delta)
    bid_size: float                  # resting size at the bid
    ask_size: float                  # resting size at the ask
    imbalance: float                 # (bid - ask) / (bid + ask); + = bid-heavy
    note: str

    @property
    def direction(self) -> Direction | None:
        """Order-book tilt as a coarse lean (only when it clears the threshold)."""
        if self.imbalance >= CONFIG.intermarket.es_imbalance_min:
            return Direction.BULLISH
        if self.imbalance <= -CONFIG.intermarket.es_imbalance_min:
            return Direction.BEARISH
        return None


class ESTracker:
    """Stateful: turns the cumulative-volume field of successive /ES Level-1
    quotes into per-interval flow (mirrors VannaTracker / bot._last_spy_cum_volume).
    Updated once per macro poll (~30s)."""

    def __init__(self) -> None:
        self._prev_cum_vol: float | None = None

    def update(self, es_quote: dict[str, Any]) -> ESReading:
        """`es_quote` is the inner Schwab `quote` block for /ES."""
        last = _num(es_quote, "lastPrice", "mark", "closePrice")
        pct = _num(es_quote, "netPercentChange")
        bid_size = _num(es_quote, "bidSize")
        ask_size = _num(es_quote, "askSize")
        cum_vol = _num(es_quote, "totalVolume")

        # Flow = interval delta of cumulative volume; clamp at 0 so the daily
        # reset (today's cumulative < the prior reading) can't fake a surge.
        if self._prev_cum_vol is not None:
            flow = max(0.0, cum_vol - self._prev_cum_vol)
        else:
            flow = 0.0
        self._prev_cum_vol = cum_vol

        denom = bid_size + ask_size
        imbalance = (bid_size - ask_size) / denom if denom > 0 else 0.0

        tilt = (
            "bid-heavy (support)" if imbalance >= CONFIG.intermarket.es_imbalance_min
            else "ask-heavy (supply)" if imbalance <= -CONFIG.intermarket.es_imbalance_min
            else "balanced"
        )
        note = (
            f"/ES {pct:+.2f}% | flow {flow:,.0f} | "
            f"bid {bid_size:,.0f} / ask {ask_size:,.0f} ({imbalance:+.2f} {tilt})"
        )
        return ESReading(
            last=last,
            pct_change=pct,
            volume_flow=flow,
            bid_size=bid_size,
            ask_size=ask_size,
            imbalance=round(imbalance, 3),
            note=note,
        )


# --------------------------------------------------------------------------- #
# SPY / QQQ legs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IntermarketLeg:
    symbol: str
    last: float
    pct_change: float                # intraday % vs prior close
    regime: str                      # GexProfile.regime ("UNKNOWN" if no chain)
    net_gex_sign: int                # +1 / -1 / 0
    zero_gamma: float | None
    direction: Direction | None      # momentum lean from pct_change

    @property
    def confirms_label(self) -> str:
        return self.direction.value if self.direction else "NEUTRAL"


def _build_leg(
    symbol: str,
    profile: GexProfile | None,
    macro_quotes: dict[str, Any],
    cfg: IntermarketConfig,
) -> IntermarketLeg:
    """One leg from its (optional) GexProfile + its Level-1 quote. Degrades to
    quote-only — the directional lean still comes through from pct_change — when
    the 0DTE chain is empty/unavailable."""
    q = _quote(macro_quotes, symbol)
    pct = _num(q, "netPercentChange")
    if profile is not None and profile.spot > 0:
        last = profile.spot
        regime = profile.regime
        net_sign = int(math.copysign(1, profile.total_net_gex)) if profile.total_net_gex else 0
        zero_g = profile.zero_gamma
    else:
        last = _num(q, "lastPrice", "mark", "closePrice")
        regime, net_sign, zero_g = "UNKNOWN", 0, None
    return IntermarketLeg(
        symbol=symbol,
        last=last,
        pct_change=pct,
        regime=regime,
        net_gex_sign=net_sign,
        zero_gamma=zero_g,
        direction=_leg_direction(pct, cfg.pct_deadband),
    )


# --------------------------------------------------------------------------- #
# Profile + scoring
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class IntermarketProfile:
    es: ESReading | None
    spy: IntermarketLeg | None
    qqq: IntermarketLeg | None
    spx_regime: str                  # reference SPX regime for the agreement check

    def alignment(self, signal_dir: Direction) -> str:
        """ALIGNED / PARTIAL / DIVERGENT / NEUTRAL against the signal direction.

        QQQ is the lead tell. ALIGNED needs QQQ + SPY to both confirm the
        direction AND QQQ's gamma regime to match SPX's. QQQ opposing = DIVERGENT;
        QQQ flat/absent = NEUTRAL."""
        qqq = self.qqq
        if qqq is None or qqq.direction is None:
            return "NEUTRAL"
        if qqq.direction != signal_dir:
            return "DIVERGENT"
        spy_confirms = self.spy is not None and self.spy.direction == signal_dir
        base = _regime_base(qqq.regime)
        regime_ok = base != "UNKNOWN" and base == _regime_base(self.spx_regime)
        if spy_confirms and regime_ok:
            return "ALIGNED"
        return "PARTIAL"

    def confidence_boost(
        self, signal_dir: Direction, cfg: IntermarketConfig | None = None
    ) -> float:
        """Bounded points to add to Claude's confidence (clamp to 95 at the call
        site). Mechanical and transparent — see the module docstring."""
        cfg = cfg or CONFIG.intermarket
        verdict = self.alignment(signal_dir)
        if verdict == "ALIGNED":
            return cfg.align_boost_max
        if verdict == "PARTIAL":
            return cfg.align_boost_max * cfg.partial_frac
        if verdict == "DIVERGENT":
            return cfg.divergence_adj
        return 0.0

    def summary(self, signal_dir: Direction) -> str:
        """One-line human read for notes/embeds (verdict + legs + ES tilt)."""
        verdict = self.alignment(signal_dir)
        legs = []
        for leg in (self.qqq, self.spy):
            if leg is not None:
                legs.append(f"{leg.symbol} {leg.pct_change:+.2f}%/{_regime_base(leg.regime)[:3]}")
        leg_txt = ", ".join(legs) if legs else "no SPY/QQQ read"
        es_txt = f"; ES {self.es.imbalance:+.2f}" if self.es is not None else ""
        return f"{verdict} ({leg_txt}{es_txt})"


def build_intermarket(
    es_reading: ESReading | None,
    spy_profile: GexProfile | None,
    qqq_profile: GexProfile | None,
    spx_profile: GexProfile | None,
    macro_quotes: dict[str, Any],
    *,
    cfg: IntermarketConfig | None = None,
) -> IntermarketProfile:
    """Assemble the intermarket read from the futures microstructure, the SPY/QQQ
    GexProfiles, the reference SPX profile, and the macro quote payload (for the
    SPY/QQQ %-change legs). Pure & synchronous."""
    cfg = cfg or CONFIG.intermarket
    return IntermarketProfile(
        es=es_reading,
        spy=_build_leg(cfg.spy_symbol, spy_profile, macro_quotes, cfg),
        qqq=_build_leg(cfg.qqq_symbol, qqq_profile, macro_quotes, cfg),
        spx_regime=spx_profile.regime if spx_profile is not None else "UNKNOWN",
    )
