"""
The analysis brain — Claude reads the structured market state and renders the
verdict that scan2's mechanical engine used to compute by formula.

Flow:
    scan2.detect_candidate(...)  ->  Candidate  (cheap gate: "ask Claude?")
    ClaudeAnalyst.analyze(...)   ->  Signal | None  (the actual judgment)

The GEX math (engine/gex.py) and the rolling tape (MarketState) stay
deterministic — they are Claude's *input*, not its job. This module is the only
place in the engine that does network I/O.

Cost control: the gate keeps calls sparse (~dozens/session), and the large,
stable methodology prompt is cache-flagged so clustered calls read it cheaply
instead of re-paying for it each time.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from anthropic import AsyncAnthropic

from helenus.config import ANTHROPIC_API_KEY, CONFIG
from helenus.engine.flow import VannaReading, VolumeProfile
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import (
    Candidate,
    Direction,
    KeyLevel,
    MarketState,
    Signal,
    TriggerType,
)

log = logging.getLogger("helenus.analyst")


SYSTEM_PROMPT = """\
You are Helenus, a disciplined intraday analyst for 0DTE SPX (PM-settled $SPXW) \
structure. You read dealer-gamma structure, key price levels, the tape, and the \
macro board, then decide whether a *tradeable* setup is present. You do not place \
trades and you never give financial advice — you read; the trader aims.

HOW TO READ THE DATA YOU ARE GIVEN
- Net GEX / regime: POSITIVE_GAMMA means dealers dampen moves (mean-reverting, \
  fade extensions toward the zero-gamma flip). NEGATIVE_GAMMA means dealers \
  amplify moves (trend/expansion, breakouts run). The zero-gamma (zero-Γ) strike \
  is the regime pivot.
- Call walls = gamma resistance (price tends to pin/reject below). Put walls = \
  gamma support. Proximity of spot to a wall is high-information confluence.
- Key levels: round-number grid, prior close, session high/low, session VWAP. A \
  reaction *at* a level matters more than one in open air.
- VWAP: the session's volume-weighted average price and the day's gravity line. \
  Whether spot is above or below it is the default intraday bias (above = buyers \
  in control, below = sellers); first tags of VWAP from one side often react, and \
  a decisive reclaim/loss of VWAP on volume is a genuine momentum shift. The \
  `vwap` block gives value, distance, and side.
- SPY-proxy volume: SPX prints no volume, so this ratio is an SPY delta vs a \
  20-bar baseline. >1 means participation is picking up; conviction, not proof.
- Trend: close vs the 20-bar mean. "With trend" setups are higher quality than \
  counter-trend ones; say so.
- Session phase / clock: the `session` block gives the time, the phase \
  (OPENING_DRIVE / MORNING_TREND / MIDDAY_LULL / AFTERNOON / POWER_HOUR), and the \
  day's range so far. Weight it — opening-drive and power-hour moves trend and \
  expand (give breaks more benefit of the doubt); MIDDAY_LULL setups fade more \
  and need tighter confluence to be worth an alert.

OPTIONS FLOW & THE VANNA RALLY — WEIGHT THIS HEAVILY
This is one of your single most important inputs. You are given 0DTE option \
volume split into ITM/OTM for calls and puts, plus where volume sits above vs \
below spot, plus a live vanna reading.
- `otm_call_put_ratio` rising means traders are paying up for upside lottery \
  tickets — directional speculation, not hedging. Heavy OTM *put* volume below \
  spot is the opposite (downside demand / hedging).
- THE VANNA RALLY: when VIX falls, implied vol drops, so calls get cheaper and \
  more affordable — buyers pile into OTM calls. Dealers short those calls must \
  buy the underlying to stay hedged (vanna), which mechanically lifts spot. The \
  `vanna` block tells you if this is live: `vix_falling` + OTM call *flow* (fresh \
  interval volume) outpacing OTM put flow. When `vanna.active` is true, treat it \
  as a strong bullish influence — ESPECIALLY if the tape has been falling hard, \
  because that is the classic reversal: sellers exhaust, VIX rolls over, call \
  flow + dealer hedging snaps price back up. A falling market with an active \
  vanna setup is a high-conviction long signal, not a reason to stay bearish.
- PUT-FLOW PRESSURE (the bearish mirror): when VIX *rises*, puts are the demand — \
  buyers pile into OTM puts and dealers short those puts hedge by SELLING the \
  underlying, pressuring spot down. `vanna.bearish_active` flags this (`vix \
  rising` + OTM put flow outpacing call flow); treat it as a strong bearish \
  influence, especially into a rally that's stalling at resistance.
- Conversely, do not call a bottom on a weak tape WITHOUT a vanna/flow tailwind, \
  or a top on a strong tape without put-flow/VIX confirmation; say what's missing.

YOUR JOB
A cheap mechanical gate already fired — an event happened (a level cross, a \
sweep). That does NOT mean it is tradeable. Decide whether the confluence of \
structure + level + volume + trend + regime actually warrants an alert.
- has_signal=false is the correct, common answer for marginal or conflicted setups. \
  Be selective; a quiet bot that only speaks on real structure is the goal.
- When has_signal=true, give the directional read, a calibrated confidence \
  (0-95 — never claim certainty), a tight one-paragraph thesis grounded in the \
  specific levels/walls/regime in front of you, and explicit risk_flags \
  (what would invalidate it, what's conflicting).

Respond ONLY with the JSON object the schema defines. No preamble.\
"""

# Prepended to the learned-lessons block when one exists. Lessons are empirical
# (graded MFE/MAE outcomes of your own past calls) — strong priors, not overrides.
LESSONS_HEADER = (
    "LEARNED LESSONS — distilled from the strict MFE/MAE grades of your own past "
    "calls on this market. Treat them as strong empirical priors: lean toward what "
    "has graded out accurate and be skeptical of what has graded out poorly. Live "
    "structure still rules when it clearly conflicts.\n\n"
)

# Structured-output schema. Numerical bounds (min/max) aren't enforceable here,
# so confidence is clamped client-side.
_SIGNAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "has_signal": {"type": "boolean"},
        "direction": {"type": "string", "enum": ["BULLISH", "BEARISH"]},
        "confidence": {"type": "integer"},
        "thesis": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["has_signal", "direction", "confidence", "thesis", "risk_flags"],
    "additionalProperties": False,
}

# Schema for the periodic accuracy review.
_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "accurate_patterns": {"type": "array", "items": {"type": "string"}},
        "inaccurate_patterns": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "summary",
        "accurate_patterns",
        "inaccurate_patterns",
        "suggestions",
    ],
    "additionalProperties": False,
}


def _fmt_walls(walls: list[tuple[float, float]]) -> list[dict[str, float]]:
    return [{"strike": round(k, 0), "net_gex": round(v)} for k, v in walls]


def _macro_board(macro: dict[str, Any]) -> dict[str, Any]:
    """Pull the handful of macro fields out of the raw Schwab quote payload."""
    def q(sym: str) -> dict:
        return (macro.get(sym, {}) or {}).get("quote") or {}

    vix = q("$VIX")
    cl = q("/CL")
    gc = q("/GC")
    return {
        "vix_last": vix.get("lastPrice"),
        "cl_pct": cl.get("netPercentChange"),
        "gc_pct": gc.get("netPercentChange"),
    }


def _session_phase(t: dt.time) -> str:
    """The 0DTE day has distinct regimes; which one we're in shapes how a setup
    should be read (open-drive trends, lunch chops, power-hour expands)."""
    if t < dt.time(10, 0):
        return "OPENING_DRIVE"
    if t < dt.time(11, 30):
        return "MORNING_TREND"
    if t < dt.time(14, 0):
        return "MIDDAY_LULL"
    if t < dt.time(15, 0):
        return "AFTERNOON"
    return "POWER_HOUR"


def _flow_block(vp: VolumeProfile | None, vanna: VannaReading | None) -> dict[str, Any]:
    """The options-flow + vanna section — Claude's highest-weight input."""
    if vp is None:
        return {"available": False}
    block: dict[str, Any] = {
        "available": True,
        "otm_call_vol": round(vp.otm_call_vol),
        "itm_call_vol": round(vp.itm_call_vol),
        "otm_put_vol": round(vp.otm_put_vol),
        "itm_put_vol": round(vp.itm_put_vol),
        "total_call_vol": round(vp.total_call_vol),
        "total_put_vol": round(vp.total_put_vol),
        "call_put_ratio": round(vp.call_put_ratio, 2),
        "otm_call_put_ratio": round(vp.otm_call_put_ratio, 2),
        "volume_above_spot": round(vp.above_spot_vol),
        "volume_below_spot": round(vp.below_spot_vol),
    }
    if vanna is not None:
        block["vanna"] = {
            "active": vanna.active,
            "bearish_active": vanna.bearish_active,
            "label": vanna.label,
            "vix_change": vanna.vix_change,
            "vix_falling": vanna.vix_falling,
            "otm_call_flow": round(vanna.otm_call_flow),
            "otm_put_flow": round(vanna.otm_put_flow),
            "note": vanna.note,
        }
    return block


def _snapshot(
    state: MarketState,
    profile: GexProfile,
    vol_profile: VolumeProfile | None,
    vanna: VannaReading | None,
    macro: dict[str, Any],
    candidate: Candidate,
) -> dict[str, Any]:
    """Compact, structured read handed to Claude as the user turn."""
    spot = profile.spot
    levels = [
        {
            "label": lv.label,
            "price": round(lv.price, 2),
            "dist_pts": round(lv.price - spot, 2),
            "weight": lv.weight,
        }
        for lv in state.key_levels(spot, profile)
    ]
    bars = [
        {
            "t": b.ts.strftime("%H:%M"),
            "o": round(b.open, 2),
            "h": round(b.high, 2),
            "l": round(b.low, 2),
            "c": round(b.close, 2),
            "v": round(b.volume),
        }
        for b in list(state.bars)[-CONFIG.analyst.recent_bars:]
    ]
    trend = state.trend_direction()
    vwap = state.vwap()
    last_ts = state.bars[-1].ts if state.bars else None
    hi, lo = state.session_high, state.session_low
    session = {
        "time_et": last_ts.strftime("%H:%M") if last_ts else None,
        "phase": _session_phase(last_ts.time()) if last_ts else None,
        "high": round(hi, 2) if hi > -1e17 else None,
        "low": round(lo, 2) if lo < 1e17 else None,
        "range_pts": round(hi - lo, 2) if (hi > -1e17 and lo < 1e17) else None,
        "bars_since_open": len(state.bars),
    }
    return {
        "spot": round(spot, 2),
        "session": session,
        "vwap": {
            "value": round(vwap, 2),
            "dist_pts": round(spot - vwap, 2),
            "side": "above" if spot >= vwap else "below",
        }
        if vwap == vwap  # not NaN
        else None,
        "gex": {
            "regime": profile.regime,
            "zero_gamma": round(profile.zero_gamma, 1)
            if profile.zero_gamma is not None
            else None,
            "total_net_gex": round(profile.total_net_gex),
            "nearest_wall_dist_pts": round(profile.nearest_cluster_distance(spot), 2),
            "call_walls": _fmt_walls(profile.call_walls),
            "put_walls": _fmt_walls(profile.put_walls),
        },
        "tape": bars,
        "options_flow": _flow_block(vol_profile, vanna),
        "volume_ratio_vs_20ma": round(state.volume_ratio(), 2)
        if state.volume_ratio() == state.volume_ratio()  # not NaN
        else None,
        "trend": trend.value if trend else None,
        "key_levels": levels,
        "gate_event": candidate.reason,
        "gate_pattern": candidate.trigger.value,
    }


class ClaudeAnalyst:
    """Owns the AsyncAnthropic client and turns state into Signals."""

    def __init__(self) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        self.client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        self.lessons: str = ""           # learned guidance fed back into the prompt

    def set_lessons(self, text: str) -> None:
        """Update the learned-lessons block injected into live-judgment prompts."""
        self.lessons = (text or "").strip()

    def _system(self, include_lessons: bool = True) -> list[dict[str, Any]]:
        """System blocks: cached methodology first, then volatile lessons (if any).

        The cache breakpoint sits on the stable methodology block, so updating
        lessons after a review doesn't invalidate the cached prefix.
        """
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if include_lessons and self.lessons:
            blocks.append({"type": "text", "text": LESSONS_HEADER + self.lessons})
        return blocks

    async def _verdict(self, user_text: str) -> dict[str, Any] | None:
        """One structured call. Returns the parsed verdict dict, or None on error."""
        try:
            resp = await self.client.messages.create(
                model=CONFIG.analyst.model,
                max_tokens=CONFIG.analyst.max_tokens,
                thinking={"type": "adaptive"},
                system=self._system(),
                messages=[{"role": "user", "content": user_text}],
                output_config={
                    "format": {"type": "json_schema", "schema": _SIGNAL_SCHEMA}
                },
            )
        except Exception:
            log.exception("Claude call failed")
            return None

        # output_config guarantees the first text block is schema-valid JSON.
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            log.warning("No text block in Claude response")
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Claude returned non-JSON: %.200s", text)
            return None

    def _trend_label(self, direction: Direction, state: MarketState) -> str:
        trend = state.trend_direction()
        if trend is None:
            return f"{direction.value} (NO TREND READ)"
        if trend == direction:
            return f"{direction.value} WITH TREND"
        return f"{direction.value} COUNTER-TREND"

    # ------------------------------------------------------------------ #
    # Intraday signal
    # ------------------------------------------------------------------ #

    async def analyze(
        self,
        state: MarketState,
        profile: GexProfile,
        vol_profile: VolumeProfile | None,
        vanna: VannaReading | None,
        macro: dict[str, Any],
        candidate: Candidate,
    ) -> Signal | None:
        """Judge a gated candidate. Returns a Signal to alert, or None to stay quiet."""
        snap = _snapshot(state, profile, vol_profile, vanna, macro, candidate)
        macro_board = _macro_board(macro)
        user = (
            "Intraday market snapshot:\n"
            + json.dumps(snap, indent=2)
            + "\n\nMacro board:\n"
            + json.dumps(macro_board, indent=2)
            + f"\n\nA {candidate.trigger.value} candidate just tripped the gate: "
            f"{candidate.reason}\n"
            "Decide whether this is a tradeable signal worth alerting."
        )
        verdict = await self._verdict(user)
        if not verdict or not verdict.get("has_signal"):
            return None

        try:
            direction = Direction(verdict["direction"])
        except (KeyError, ValueError):
            log.warning("Bad direction in verdict: %s", verdict)
            return None
        confidence = max(0.0, min(float(verdict.get("confidence", 0)), 95.0))
        notes = [verdict.get("thesis", "").strip()]
        notes += [f"⚠ {r}" for r in verdict.get("risk_flags", []) if r]

        return Signal(
            trigger=candidate.trigger,
            direction=direction,
            level=candidate.level,
            spot=profile.spot,
            volume_ratio=state.volume_ratio(),
            confidence=confidence,
            trend_label=self._trend_label(direction, state),
            notes=[n for n in notes if n],
        )

    # ------------------------------------------------------------------ #
    # Pre-market briefing
    # ------------------------------------------------------------------ #

    async def premarket_briefing(
        self,
        es: dict[str, Any],
        cl: dict[str, Any],
        vix_last: float,
        vix_band: tuple[float, float],
    ) -> tuple[Signal, list[str]]:
        """Overnight read. Always returns a Signal — the briefing is the product."""
        es_last = float(es.get("lastPrice") or 0.0)
        es_prior = float(es.get("closePrice") or 0.0)
        es_pct = (es_last / es_prior - 1.0) * 100 if es_prior else 0.0
        cl_pct = float(cl.get("netPercentChange") or 0.0)

        context = {
            "es_last": es_last,
            "es_pct_vs_prior_close": round(es_pct, 2),
            "vix_last": round(vix_last, 2),
            "vix_band": [round(vix_band[0], 2), round(vix_band[1], 2)],
            "cl_pct": round(cl_pct, 2),
        }
        user = (
            "Pre-market overnight context (before the 09:30 ET open):\n"
            + json.dumps(context, indent=2)
            + "\n\nGive the overnight bias briefing: lean, conviction, and what to "
            "watch. has_signal must be true (the briefing always publishes)."
        )
        verdict = await self._verdict(user)

        macro_lines = [
            f"/ES `{es.get('lastPrice')}` ({es_pct:+.2f}%)",
            f"/CL `{cl.get('lastPrice')}` ({cl_pct:+.2f}%)",
            f"$VIX `{vix_last:.2f}` band {vix_band[0]:.2f}–{vix_band[1]:.2f}",
        ]

        if not verdict:
            # Fail safe: never silently drop the briefing.
            sig = Signal(
                trigger=TriggerType.PREMARKET_SETUP,
                direction=Direction.BULLISH if es_pct >= 0 else Direction.BEARISH,
                level=None,
                spot=es_last,
                volume_ratio=float("nan"),
                confidence=0.0,
                trend_label="OVERNIGHT BIAS (analysis unavailable)",
                notes=["Claude analysis unavailable — showing raw overnight tape only."],
            )
            return sig, macro_lines

        direction = Direction(verdict.get("direction", "BULLISH"))
        confidence = max(0.0, min(float(verdict.get("confidence", 0)), 95.0))
        notes = [verdict.get("thesis", "").strip()]
        notes += [f"⚠ {r}" for r in verdict.get("risk_flags", []) if r]
        sig = Signal(
            trigger=TriggerType.PREMARKET_SETUP,
            direction=direction,
            level=None,
            spot=es_last,
            volume_ratio=float("nan"),
            confidence=confidence,
            trend_label=f"{direction.value} OVERNIGHT BIAS",
            notes=[n for n in notes if n],
        )
        return sig, macro_lines

    # ------------------------------------------------------------------ #
    # Accuracy feedback loop
    # ------------------------------------------------------------------ #

    async def reflect(self, alert: dict[str, Any], outcome: dict[str, Any]) -> str | None:
        """One matured alert + its MFE/MAE result -> a short 'why' note. Cheap."""
        user = (
            "An alert has matured. Here is the setup that fired and the strict "
            "MFE/MAE price-action result that followed:\n\n"
            "Alert:\n" + json.dumps(alert, indent=2)
            + "\n\nOutcome (graded on SPX price only):\n" + json.dumps(outcome, indent=2)
            + "\n\nIn 1-2 sentences: what specifically about this setup most likely "
            "drove the result? Name a concrete, trackable pattern (e.g. 'vanna "
            "active in negative-gamma followed through' or 'counter-trend cross "
            "into a call wall stalled'). Plain text, no preamble."
        )
        try:
            resp = await self.client.messages.create(
                model=CONFIG.analyst.model,
                max_tokens=512,
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
                system=self._system(),
                messages=[{"role": "user", "content": user}],
            )
        except Exception:
            log.exception("reflect() call failed")
            return None
        return next((b.text for b in resp.content if b.type == "text"), None)

    async def review_patterns(self, graded: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Read a batch of graded alerts and surface accurate/inaccurate patterns."""
        user = (
            "Below are recent Helenus alerts, each with its full setup context and "
            "the strict MFE/MAE grade earned on SPX price action "
            "(ACCURATE / MIXED / INACCURATE). Find the patterns that separate the "
            "accurate alerts from the inaccurate ones — focus on structure, regime, "
            "options flow / vanna state, trend alignment, and confidence vs outcome. "
            "Be concrete and specific; these notes are used to tune the engine.\n\n"
            + json.dumps(graded, indent=2)
        )
        try:
            resp = await self.client.messages.create(
                model=CONFIG.analyst.model,
                max_tokens=CONFIG.analyst.max_tokens,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": _REVIEW_SCHEMA},
                },
                system=self._system(include_lessons=False),
                messages=[{"role": "user", "content": user}],
            )
        except Exception:
            log.exception("review_patterns() call failed")
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("Review returned non-JSON: %.200s", text)
            return None

    async def aclose(self) -> None:
        try:
            await self.client.close()
        except Exception:
            log.debug("AsyncAnthropic close failed", exc_info=True)
