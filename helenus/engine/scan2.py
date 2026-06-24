"""
scan2 — the mechanical *candidate* layer. Pure, no I/O, no awaits.

This module no longer renders verdicts. It computes the rolling intraday tape
(MarketState), the key-level grid, and a cheap gate (`detect_candidate`) that
decides *when* a bar is interesting enough to spend a Claude call on. The
judgment — direction, confidence, thesis — is made downstream in
`helenus.engine.analyst`. The `Signal` dataclass here is the shared shape both
layers speak; Claude fills it in.

Candidate patterns the gate watches:
  * VOLUME_CONFIRMATION — spot crosses a key structural level on elevated volume.
  * SWEEP_RECOVER — spot pierces a key level intra-bar, then the very next
    sequential bar closes back on the original side (liquidity sweep).
  * LEVEL_REJECTION — spot probes a high-value level (session extreme, prior
    close, GEX wall, zero-Γ) and is turned away within the bar, leaving a
    rejection wick. The range-day reversal the deep-pierce sweep test misses.
  * RANGE_EXPANSION — a directional thrust between levels (net displacement over
    the lookback clears an ATR multiple); the trend-day momentum signal.
  * REGIME_FLIP — spot crosses the zero-gamma pivot (mean-revert ↔ expansion).
  * VANNA_RALLY / PUT_FLOW — options-flow events (VIX + OTM call/put flow).
  * PREMARKET_SETUP — handled directly by the analyst before the open.

Key levels tracked: round-number grid, prior-day close, session high/low,
session VWAP, plus GEX walls and charm (delta-decay) support/resistance walls
injected from the gamma/charm engines for confluence.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from helenus.config import CONFIG
from helenus.engine.charm import CharmProfile
from helenus.engine.gex import GexProfile

if TYPE_CHECKING:
    from helenus.engine.flow import VannaReading
    from helenus.engine.scalp import ScalpReading
    from helenus.engine.displacement import DisplacementReading
    from helenus.engine.orb import ORBReading
    from helenus.engine.moc import MocReading


class Direction(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class TriggerType(Enum):
    VOLUME_CONFIRMATION = "Volume Confirmation"
    SWEEP_RECOVER = "Sweep & Recover"
    LEVEL_REJECTION = "Level Rejection"
    RANGE_EXPANSION = "Range Expansion"
    REGIME_FLIP = "Regime Flip"
    PREMARKET_SETUP = "Pre-Market Setup"
    VANNA_RALLY = "Vanna Momentum"
    PUT_FLOW = "Put Flow Pressure"
    EMA_IGNITION = "EMA Ignition"
    DISPLACEMENT = "Displacement"
    ORB_BREAKOUT = "ORB Breakout"
    MOC_REVERSAL = "MOC Reversal"
    CAPITULATION = "Capitulation Candle"


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
    confidence: float           # 0..100, assigned by Claude
    trend_label: str            # e.g. "BULLISH WITH TREND"
    notes: list[str] = field(default_factory=list)
    # The EMA-ignition scalp read attached when the gate fired on a 5/9 cross —
    # carries the delta-targeted strike + premium target for the embed/journal.
    scalp: "ScalpReading | None" = None


@dataclass(frozen=True)
class Candidate:
    """A bar the gate flagged as worth a Claude call. Not a verdict."""
    trigger: TriggerType
    level: KeyLevel | None
    reason: str                 # human-readable description of what tripped


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
        # Re-test counters: how many DISTINCT times the running session extreme has
        # been re-tagged and held (price returned to within proximity after moving
        # off it) — the "exhausted shelf" tell. A new extreme resets its count;
        # consecutive bars hugging the level count once, not per bar. Session-
        # cumulative (the whole afternoon), like VWAP — see _away flags below.
        self.high_retests: int = 0
        self.low_retests: int = 0
        self._high_away: bool = False
        self._low_away: bool = False
        # Session-cumulative typical-price×volume and volume, for VWAP. These run
        # over the whole session (not the bounded `bars` deque), so VWAP stays
        # correct even after old bars roll off — reset only by a new session.
        self._cum_pv: float = 0.0
        self._cum_vol: float = 0.0

    def push_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        prox = CONFIG.scan.level_proximity_pts
        # Session-low shelf: a new low establishes a fresh shelf; a bar that returns
        # to within proximity AFTER price had moved off it is a distinct re-test
        # that held (a consolidation hugging the low counts once, not per bar).
        if bar.low < self.session_low:
            self.session_low = bar.low
            self.low_retests = 0
            self._low_away = False
        elif np.isfinite(self.session_low):
            if bar.low <= self.session_low + prox:
                if self._low_away:
                    self.low_retests += 1
                    self._low_away = False
            else:
                self._low_away = True
        # Session-high shelf — mirror.
        if bar.high > self.session_high:
            self.session_high = bar.high
            self.high_retests = 0
            self._high_away = False
        elif np.isfinite(self.session_high):
            if bar.high >= self.session_high - prox:
                if self._high_away:
                    self.high_retests += 1
                    self._high_away = False
            else:
                self._high_away = True
        typical = (bar.high + bar.low + bar.close) / 3.0
        self._cum_pv += typical * bar.volume
        self._cum_vol += bar.volume

    # -- baselines --------------------------------------------------------- #

    def volume_baseline(self) -> float:
        """Mean interval volume over the trailing window (excl. the current bar).

        Uses the full 20-period window once warm, but falls back to whatever
        history exists (down to `min_baseline_bars` priors) during the opening
        ramp — otherwise the first ~20 minutes are structurally un-alertable,
        which is exactly when session-defining breaks (e.g. the open-drive high
        rejection) happen.
        """
        n = CONFIG.scan.volume_ma_periods
        priors = len(self.bars) - 1  # exclude the current (forming) bar
        if priors < CONFIG.scan.min_baseline_bars:
            return float("nan")
        window = min(n, priors)
        vols = pd.Series([b.volume for b in self.bars])
        return float(vols.iloc[-(window + 1):-1].mean())

    def volume_ratio(self) -> float:
        base = self.volume_baseline()
        if not np.isfinite(base) or base <= 0 or not self.bars:
            return float("nan")
        return self.bars[-1].volume / base

    def atr(self, n: int | None = None) -> float:
        """Mean bar range (high-low) over the last n bars, excluding the current
        forming one — a cheap ATR for the range-expansion trigger. NaN until warm."""
        n = n or CONFIG.scan.volume_ma_periods
        priors = list(self.bars)[:-1]
        if len(priors) < CONFIG.scan.min_baseline_bars:
            return float("nan")
        window = priors[-n:]
        return float(np.mean([b.high - b.low for b in window]))

    def realized_range_5m(self) -> float:
        """Spot range over the last ~5 minutes — fed back to the throttle."""
        secs = CONFIG.scan.bar_seconds
        n = max(1, int(300 / secs))
        recent = list(self.bars)[-n:]
        if not recent:
            return 0.0
        return max(b.high for b in recent) - min(b.low for b in recent)

    def vwap(self) -> float:
        """Session VWAP from typical price weighted by SPY-proxy volume. NaN until
        any volume has accumulated. The single most-watched 0DTE reference."""
        if self._cum_vol <= 0:
            return float("nan")
        return self._cum_pv / self._cum_vol

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

    def key_levels(
        self,
        spot: float,
        gex: GexProfile | None = None,
        charm: CharmProfile | None = None,
    ) -> list[KeyLevel]:
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
        vwap = self.vwap()
        if np.isfinite(vwap):
            levels.append(KeyLevel(vwap, "VWAP", weight=0.85))

        # GEX structure — the highest-information levels the bot is built around.
        # Call/put walls are dealer-gamma resistance/support; zero-gamma is the
        # regime pivot. These are what a range-day reaction most often happens at.
        if gex is not None:
            for strike, _net in gex.call_walls:
                levels.append(
                    KeyLevel(float(strike), f"Call Wall {strike:.0f}", weight=0.85)
                )
            for strike, _net in gex.put_walls:
                levels.append(
                    KeyLevel(float(strike), f"Put Wall {strike:.0f}", weight=0.85)
                )
            if gex.zero_gamma is not None:
                levels.append(KeyLevel(float(gex.zero_gamma), "Zero-Γ", weight=0.9))

        # Charm structure — the OTM-wing delta-decay walls dealers un-hedge into.
        # Put-charm support and call-charm resistance are real reaction levels,
        # most forceful in the afternoon (charm ∝ 1/T); a reaction at one is high
        # confluence with the gamma walls it often sits near.
        if charm is not None:
            w = CONFIG.charm.wall_weight
            for strike, _c in charm.support_walls:
                levels.append(KeyLevel(float(strike), f"Charm Support {strike:.0f}", w))
            for strike, _c in charm.resistance_walls:
                levels.append(KeyLevel(float(strike), f"Charm Resist {strike:.0f}", w))
        return levels


# --------------------------------------------------------------------------- #
# Candidate gate
# --------------------------------------------------------------------------- #

def detect_candidate(
    state: MarketState,
    gex: GexProfile | None,
    vanna: "VannaReading | None" = None,
    charm: CharmProfile | None = None,
    scalp: "ScalpReading | None" = None,
    displacement: "DisplacementReading | None" = None,
    orb: "ORBReading | None" = None,
    moc: "MocReading | None" = None,
) -> Candidate | None:
    """
    Cheap attention gate: is the latest bar interesting enough to spend a Claude
    call on? Returns a Candidate describing what tripped, or None.

    This makes no directional or confidence claim — that's the analyst's job.
    The power-hour MOC plays are checked first (window-gated, so rare); then the
    general precedence is GEX regime → options flow → EMA ignition → displacement,
    with the level-based triggers behind them:
      * a MOC reversal — inside the 3:50–3:55 close window, one side's premium based
         while its volume surged vs the other (engine/moc.py); window-gated,
      * a capitulation candle — a contract's premium wicked above its 5/9/200 EMAs
         then rejected (engine/moc.py); a reversal heads-up, fires intraday,
      1. a regime flip (spot crosses the zero-gamma pivot) — the lead GEX edge,
      2. a vanna / flow trigger — a vanna rally (VIX1D falling + OTM call flow) or
         its bearish analogue, put-flow pressure (VIX1D rising + OTM put flow); a
         PRIMARY standalone signal, fires regardless of any chart pattern,
      3. an EMA ignition — a gated 1m 5/9 cross / 5-EMA reclaim (engine/scalp.py),
      4. a displacement — a high-volume institutional thrust candle (FVG / MSS /
         sweep are boosters now, not required — engine/displacement.py),
      5. an ORB breakout — a close beyond the locked opening range with volume +
         VWAP confirmation (engine/orb.py),
      6. a liquidity sweep (pierce-and-reclaim of a key level),
      7. a rejection at a high-value level (touch-and-reverse off a session
         extreme / GEX wall / zero-Γ),
      8. a range-expansion thrust between levels (trend-day momentum),
      9. a level cross on elevated volume.
    """
    # The power-hour MOC plays are checked first (window-gated). After the gex
    # guard the general precedence is regime flip → vanna/flow → EMA ignition →
    # displacement, then the level-based triggers.

    # --- MOC reversal: the power-hour close play (premium + volume) ------- #
    # Inside the 3:50–3:55 window, one side's premium based (a higher-low off a
    # decline) while its fresh volume surged vs the other — the simplified
    # premium-behavior reversal (engine/moc.py). The priority power-hour play and
    # window-gated, so it's rare; it leads the order.
    if moc is not None and moc.reversal_active:
        level = (
            KeyLevel(float(moc.nearest_wall), f"GEX {moc.gex_state}", 0.85)
            if moc.nearest_wall is not None else None
        )
        return Candidate(trigger=TriggerType.MOC_REVERSAL, level=level, reason=moc.note)

    # --- Capitulation candle: a premium wick rejection ------------------- #
    # A contract's premium candle wicked above its own 5/9/200 EMAs then rejected
    # (the "instant buy & sell book") — a reversal heads-up that resolves
    # undercut-then-reclaim. Not window-gated: it happens intraday too.
    if moc is not None and moc.capitulation:
        level = (
            KeyLevel(float(moc.nearest_wall), f"GEX {moc.gex_state}", 0.6)
            if moc.nearest_wall is not None else None
        )
        return Candidate(trigger=TriggerType.CAPITULATION, level=level, reason=moc.note)

    if gex is None or len(state.bars) < 2:
        return None

    cur = state.bars[-1]
    prev = state.bars[-2]

    # --- Regime flip: spot crosses the zero-gamma pivot ------------------ #
    # Positive↔negative gamma is the biggest structural state change there is:
    # dealers flip from damping moves (mean-reversion) to amplifying them
    # (trend/expansion), or vice-versa. A top-tier structural edge.
    if gex.zero_gamma is not None:
        zg = float(gex.zero_gamma)
        crossed_up = prev.close < zg <= cur.close
        crossed_dn = prev.close > zg >= cur.close
        if crossed_up or crossed_dn:
            regime = (
                "POSITIVE_GAMMA (mean-revert)" if crossed_up
                else "NEGATIVE_GAMMA (expansion)"
            )
            return Candidate(
                trigger=TriggerType.REGIME_FLIP,
                level=KeyLevel(zg, "Zero-Γ", 0.9),
                reason=f"Crossed zero-gamma {zg:.0f} "
                f"{'up' if crossed_up else 'down'} — regime now {regime}",
            )

    # --- Vanna / flow: a PRIMARY standalone trigger (ranked #2) ---------- #
    # Re-elevated to a primary signal, just under the regime flip: VIX1D falling
    # with fresh OTM call flow outpacing puts (dealers hedge by buying → spot
    # lifts), or the bearish mirror (VIX1D rising + OTM put flow → dealer selling).
    # Fires off the flow read ALONE — regardless of any chart pattern.
    if vanna is not None and vanna.active:
        return Candidate(trigger=TriggerType.VANNA_RALLY, level=None, reason=vanna.note)
    if vanna is not None and vanna.bearish_active:
        return Candidate(trigger=TriggerType.PUT_FLOW, level=None, reason=vanna.note)

    # --- EMA ignition: the gated 1m 5/9 contract scalp (a MAIN EDGE) ----- #
    # The 1m 5/9 momentum scalp. The scalp engine already ran its confirmation
    # stack (vanna headwind, SPX confirm on VWAP, room ≥ floor, chop, dual-bleed,
    # spread); regime is no longer a hard gate — a high-positive-GEX fire is flagged
    # `slow_grind` rather than blocked. The level it carries is the premium target.
    if scalp is not None and scalp.active:
        return Candidate(
            trigger=TriggerType.EMA_IGNITION,
            level=scalp.target_level,
            reason=scalp.note,
        )

    # --- Displacement: the high-volume institutional thrust candle ------- #
    # The candle itself is the hard gate now; FVG / MSS / sweep are conviction
    # boosters (engine/displacement.py). The level is the swing the thrust broke
    # (MSS) when present, else the candle's defended 50% midpoint.
    if displacement is not None and displacement.active:
        if displacement.mss_level is not None:
            level = KeyLevel(float(displacement.mss_level), "MSS Break", 0.85)
        elif displacement.midpoint is not None:
            level = KeyLevel(float(displacement.midpoint), "Disp 50%", 0.8)
        else:
            level = None
        return Candidate(
            trigger=TriggerType.DISPLACEMENT,
            level=level,
            reason=displacement.note,
        )

    # --- ORB breakout: a confirmed close beyond the opening range -------- #
    # The first-N-minute range is the session pivot; a close beyond it with
    # volume + VWAP confirmation is the breakout (engine/orb.py applies both
    # filters). The broken range edge is the entry level.
    if orb is not None and orb.active and orb.entry is not None:
        side = "ORB High" if orb.direction is Direction.BULLISH else "ORB Low"
        return Candidate(
            trigger=TriggerType.ORB_BREAKOUT,
            level=KeyLevel(float(orb.entry), side, 0.85),
            reason=orb.note,
        )

    # --- Sweep & recover (needs 3 bars) ---------------------------------- #
    if len(state.bars) >= 3:
        before, pierce = state.bars[-3], state.bars[-2]
        depth = CONFIG.scan.sweep_pierce_pts
        for level in state.key_levels(cur.close, gex, charm):
            swept_low = (
                before.close > level.price
                and pierce.low <= level.price - depth
                and cur.close > level.price
            )
            swept_high = (
                before.close < level.price
                and pierce.high >= level.price + depth
                and cur.close < level.price
            )
            if swept_low or swept_high:
                side = "below" if swept_low else "above"
                return Candidate(
                    trigger=TriggerType.SWEEP_RECOVER,
                    level=level,
                    reason=f"Swept {side} {level.label} @ {level.price:.2f}, reclaimed next bar",
                )

    # --- Rejection at a high-value level (touch & reverse) --------------- #
    # The range-day bread-and-butter: spot probes a meaningful level (session
    # extreme, prior close, GEX wall, zero-Γ) and is turned away within the bar,
    # leaving a rejection wick. Unlike SWEEP_RECOVER this needs only the *current*
    # bar and does NOT require a deep pierce-and-reclaim — a level that holds on
    # the wick never trips the sweep test. Restricted to weighty levels and a
    # real wick so midday chop stays quiet.
    prox = CONFIG.scan.level_proximity_pts
    wick = CONFIG.scan.rejection_wick_pts
    bar_range = cur.high - cur.low
    if bar_range >= wick:
        # Session extremes are taken as they stood BEFORE this bar (a genuine
        # retest of an established floor/ceiling). Otherwise a bar that merely
        # prints a fresh new low and closes green would tautologically "reject
        # off the session low." Walls / prior close keep first-touch semantics —
        # they're real structure independent of the tape.
        rej_levels = [
            lv
            for lv in state.key_levels(cur.close, gex, charm)
            if lv.weight >= CONFIG.scan.rejection_min_weight
            and lv.label not in ("Session High", "Session Low")
        ]
        prior = list(state.bars)[:-1]
        prior_low = min(b.low for b in prior)
        prior_high = max(b.high for b in prior)
        # Session extremes are taken as they stood BEFORE this bar (a genuine
        # retest of an established floor/ceiling). Otherwise a bar that merely
        # prints a fresh new low and closes green would tautologically "reject
        # off the session low." Walls / prior close keep first-touch semantics.
        rej_levels.append(KeyLevel(prior_low, "Session Low", weight=0.7))
        rej_levels.append(KeyLevel(prior_high, "Session High", weight=0.7))
        # Side is set by where price approached from (the prior close), so a
        # level only acts as support when we're above it and resistance when
        # below. Without this, a falling bar grazing the descending session low
        # reads as a bogus "resistance rejection" — pure trend-continuation noise.
        ref = state.bars[-2].close
        edge = CONFIG.scan.edge_proximity_pts
        for level in rej_levels:
            # Edge gate: a support rejection must sit near the running floor, a
            # resistance rejection near the running ceiling. An interior wall
            # price is merely pinning is neither, so it never fires here.
            near_floor = (level.price - prior_low) <= edge
            near_ceiling = (prior_high - level.price) <= edge
            # Bullish: wick down tags support from above, closes back over it.
            held_support = (
                near_floor
                and level.price <= ref
                and level.price - prox <= cur.low <= level.price + prox
                and cur.close > cur.low
                and cur.close >= level.price
                and (cur.close - cur.low) >= wick
            )
            # Bearish: wick up tags resistance from below, closes back under it.
            held_resist = (
                near_ceiling
                and level.price >= ref
                and level.price - prox <= cur.high <= level.price + prox
                and cur.close < cur.high
                and cur.close <= level.price
                and (cur.high - cur.close) >= wick
            )
            if held_support or held_resist:
                side = "support" if held_support else "resistance"
                return Candidate(
                    trigger=TriggerType.LEVEL_REJECTION,
                    level=level,
                    reason=f"Rejected off {level.label} @ {level.price:.2f} "
                    f"({side}); closed {cur.close:.2f}",
                )

    # --- Range expansion / momentum thrust ------------------------------- #
    # A directional push that the level-based triggers miss because it happens
    # between levels — the trend-day signal. Net displacement over the lookback
    # must clear both an ATR multiple and an absolute point floor.
    atr = state.atr()
    k = CONFIG.scan.expansion_lookback_bars
    if np.isfinite(atr) and atr > 0 and len(state.bars) > k:
        disp = cur.close - state.bars[-1 - k].close
        if (
            abs(disp) >= CONFIG.scan.expansion_min_pts
            and abs(disp) >= CONFIG.scan.expansion_atr_mult * atr
        ):
            d = "up" if disp > 0 else "down"
            return Candidate(
                trigger=TriggerType.RANGE_EXPANSION,
                level=None,
                reason=f"{abs(disp):.1f}pt {d}-thrust over {k} bars "
                f"({abs(disp) / atr:.1f}× ATR)",
            )

    # --- Level cross on elevated volume ---------------------------------- #
    ratio = state.volume_ratio()
    if np.isfinite(ratio) and ratio >= CONFIG.analyst.gate_volume_ratio:
        for level in state.key_levels(cur.close, gex, charm):
            crossed_up = prev.close < level.price <= cur.close
            crossed_dn = prev.close > level.price >= cur.close
            if crossed_up or crossed_dn:
                d = "up" if crossed_up else "down"
                return Candidate(
                    trigger=TriggerType.VOLUME_CONFIRMATION,
                    level=level,
                    reason=f"Crossed {level.label} @ {level.price:.2f} {d} on {ratio:.1f}x volume",
                )

    return None
