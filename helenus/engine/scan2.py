"""
scan2 — the mechanical scan engine. Pure state machine: feed it bars and a
GexProfile, it emits Signal objects (or nothing). No I/O, no awaits.

Triggers:
  1. VOLUME_CONFIRMATION — spot crosses a key structural level on >= 2.0x
     the 20-period volume baseline.
  2. SWEEP_RECOVER — spot pierces a key level intra-bar, then the very next
     sequential bar closes back on the original side (liquidity sweep).
  3. PREMARKET_SETUP — overnight futures location vs VIX bands and /CL trend.

Key levels tracked: round-number grid, prior-day close, session high/low,
plus GEX walls injected from the gamma engine for confluence scoring.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

from helenus.config import CONFIG
from helenus.engine.gex import GexProfile


class Direction(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class TriggerType(Enum):
    VOLUME_CONFIRMATION = "Volume Confirmation"
    SWEEP_RECOVER = "Sweep & Recover"
    PREMARKET_SETUP = "Pre-Market Setup"


@dataclass(frozen=True)
class KeyLevel:
    price: float
    label: str          # e.g. "Round 7450", "Prior Close", "Session High"
    weight: float       # confluence weight, 0..1


@dataclass(frozen=True)
class Bar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float       # interval volume (SPY proxy delta)


@dataclass(frozen=True)
class Signal:
    trigger: TriggerType
    direction: Direction
    level: KeyLevel | None
    spot: float
    volume_ratio: float
    confidence: float           # 0..100
    trend_label: str            # e.g. "BULLISH WITH TREND"
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rolling state
# --------------------------------------------------------------------------- #

class MarketState:
    """Holds the rolling intraday tape scan2 needs to evaluate triggers."""

    def __init__(self, prior_close: float | None = None) -> None:
        self.prior_close = prior_close
        self.bars: deque[Bar] = deque(maxlen=max(CONFIG.scan.volume_ma_periods * 3, 120))
        self.session_high: float = -np.inf
        self.session_low: float = np.inf

    def push_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        self.session_high = max(self.session_high, bar.high)
        self.session_low = min(self.session_low, bar.low)

    # -- baselines --------------------------------------------------------- #

    def volume_baseline(self) -> float:
        """Rolling 20-period simple MA of interval volume (excl. current bar)."""
        n = CONFIG.scan.volume_ma_periods
        if len(self.bars) < n + 1:
            return float("nan")
        vols = pd.Series([b.volume for b in self.bars])
        return float(vols.iloc[-(n + 1):-1].mean())

    def volume_ratio(self) -> float:
        base = self.volume_baseline()
        if not np.isfinite(base) or base <= 0 or not self.bars:
            return float("nan")
        return self.bars[-1].volume / base

    def realized_range_5m(self) -> float:
        """Spot range over the last ~5 minutes — fed back to the throttle."""
        secs = CONFIG.scan.bar_seconds
        n = max(1, int(300 / secs))
        recent = list(self.bars)[-n:]
        if not recent:
            return 0.0
        return max(b.high for b in recent) - min(b.low for b in recent)

    def trend_direction(self) -> Direction | None:
        """Cheap structural trend: close vs 20-bar mean close."""
        n = CONFIG.scan.volume_ma_periods
        if len(self.bars) < n:
            return None
        closes = pd.Series([b.close for b in self.bars])
        ma = float(closes.iloc[-n:].mean())
        last = closes.iloc[-1]
        if last > ma:
            return Direction.BULLISH
        if last < ma:
            return Direction.BEARISH
        return None

    # -- levels ------------------------------------------------------------ #

    def key_levels(self, spot: float) -> list[KeyLevel]:
        levels: list[KeyLevel] = []
        step = CONFIG.scan.round_number_step
        major = CONFIG.scan.major_round_step

        below = np.floor(spot / step) * step
        for k in (below - step, below, below + step, below + 2 * step):
            is_major = k % major == 0
            levels.append(
                KeyLevel(float(k), f"Round {k:.0f}", weight=0.6 if is_major else 0.35)
            )
        if self.prior_close:
            levels.append(KeyLevel(self.prior_close, "Prior Close", weight=0.8))
        if np.isfinite(self.session_high):
            levels.append(KeyLevel(self.session_high, "Session High", weight=0.7))
        if np.isfinite(self.session_low):
            levels.append(KeyLevel(self.session_low, "Session Low", weight=0.7))
        return levels


# --------------------------------------------------------------------------- #
# Confidence scoring
# --------------------------------------------------------------------------- #

def _confidence(
    volume_ratio: float,
    level: KeyLevel | None,
    gex: GexProfile | None,
    spot: float,
    direction: Direction,
    trend: Direction | None,
) -> tuple[float, str]:
    """
    Confluence model, capped at 95 — scan2 never claims certainty.

      volume component   : up to 35 pts, saturates at 4x baseline
      level weight       : up to 25 pts
      GEX cluster prox.  : up to 20 pts, linear inside proximity window
      trend alignment    : +15 with trend / +0 counter-trend
    """
    score = 0.0
    if np.isfinite(volume_ratio):
        score += 35.0 * min(volume_ratio / 4.0, 1.0)
    if level is not None:
        score += 25.0 * level.weight
    if gex is not None:
        d = gex.nearest_cluster_distance(spot)
        window = CONFIG.scan.gex_cluster_proximity_pts
        if d <= window:
            score += 20.0 * (1.0 - d / window)

    if trend is None:
        trend_label = f"{direction.value} (NO TREND READ)"
    elif trend == direction:
        score += 15.0
        trend_label = f"{direction.value} WITH TREND"
    else:
        trend_label = f"{direction.value} COUNTER-TREND"

    return min(round(score, 1), 95.0), trend_label


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #

def check_volume_confirmation(
    state: MarketState, gex: GexProfile | None
) -> Signal | None:
    """Trigger 1: level cross on >= 2.0x volume."""
    if len(state.bars) < 2:
        return None
    prev, cur = state.bars[-2], state.bars[-1]
    ratio = state.volume_ratio()
    if not np.isfinite(ratio) or ratio < CONFIG.scan.volume_trigger_ratio:
        return None

    for level in state.key_levels(cur.close):
        crossed_up = prev.close < level.price <= cur.close
        crossed_dn = prev.close > level.price >= cur.close
        if not (crossed_up or crossed_dn):
            continue
        direction = Direction.BULLISH if crossed_up else Direction.BEARISH
        conf, trend_label = _confidence(
            ratio, level, gex, cur.close, direction, state.trend_direction()
        )
        return Signal(
            trigger=TriggerType.VOLUME_CONFIRMATION,
            direction=direction,
            level=level,
            spot=cur.close,
            volume_ratio=ratio,
            confidence=conf,
            trend_label=trend_label,
            notes=[f"Crossed {level.label} @ {level.price:.2f} on {ratio:.1f}x volume"],
        )
    return None


def check_sweep_recover(state: MarketState, gex: GexProfile | None) -> Signal | None:
    """
    Trigger 2: bar N pierces a level by >= sweep_pierce_pts but the very next
    bar (N+1, the current bar) closes back on the original side.
    """
    if len(state.bars) < 3:
        return None
    before, pierce, cur = state.bars[-3], state.bars[-2], state.bars[-1]
    depth = CONFIG.scan.sweep_pierce_pts

    for level in state.key_levels(cur.close):
        # Sweep below support -> bullish reclaim
        swept_low = (
            before.close > level.price
            and pierce.low <= level.price - depth
            and cur.close > level.price
        )
        # Sweep above resistance -> bearish rejection
        swept_high = (
            before.close < level.price
            and pierce.high >= level.price + depth
            and cur.close < level.price
        )
        if not (swept_low or swept_high):
            continue
        direction = Direction.BULLISH if swept_low else Direction.BEARISH
        ratio = state.volume_ratio()
        conf, trend_label = _confidence(
            ratio, level, gex, cur.close, direction, state.trend_direction()
        )
        side = "below" if swept_low else "above"
        return Signal(
            trigger=TriggerType.SWEEP_RECOVER,
            direction=direction,
            level=level,
            spot=cur.close,
            volume_ratio=ratio,
            confidence=conf,
            trend_label=trend_label,
            notes=[f"Swept {side} {level.label} @ {level.price:.2f}, reclaimed next bar"],
        )
    return None


def check_premarket_setup(
    es_last: float,
    es_prior_close: float,
    vix_last: float,
    vix_band: tuple[float, float],
    cl_change_pct: float,
) -> Signal:
    """
    Trigger 3: pre-market briefing. Always emits — the briefing itself is the
    product; direction reflects the overnight balance of evidence.

    Inputs:
        es_last / es_prior_close : overnight futures location
        vix_band                 : (low, high) recent VIX range boundaries
        cl_change_pct            : /CL overnight % change (risk-tone proxy)
    """
    notes: list[str] = []
    score = 0

    es_pct = (es_last / es_prior_close - 1.0) * 100 if es_prior_close else 0.0
    score += 1 if es_pct > 0 else -1
    notes.append(f"Futures {es_pct:+.2f}% vs prior close")

    vix_lo, vix_hi = vix_band
    if vix_last <= vix_lo:
        score += 1
        notes.append(f"VIX {vix_last:.2f} at/below range low {vix_lo:.2f} — supportive")
    elif vix_last >= vix_hi:
        score -= 1
        notes.append(f"VIX {vix_last:.2f} at/above range high {vix_hi:.2f} — defensive")
    else:
        notes.append(f"VIX {vix_last:.2f} mid-range ({vix_lo:.2f}–{vix_hi:.2f})")

    if abs(cl_change_pct) >= 1.0:
        score += 1 if cl_change_pct > 0 else -1
        notes.append(f"/CL {cl_change_pct:+.2f}% overnight — macro tone driver")
    else:
        notes.append(f"/CL {cl_change_pct:+.2f}% — quiet")

    direction = Direction.BULLISH if score >= 0 else Direction.BEARISH
    confidence = min(40.0 + 15.0 * abs(score), 85.0)
    return Signal(
        trigger=TriggerType.PREMARKET_SETUP,
        direction=direction,
        level=None,
        spot=es_last,
        volume_ratio=float("nan"),
        confidence=confidence,
        trend_label=f"{direction.value} OVERNIGHT BIAS",
        notes=notes,
    )
