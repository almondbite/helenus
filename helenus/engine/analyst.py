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
from helenus.engine.charm import CharmProfile
from helenus.engine.flow import VannaReading, VolumeProfile
from helenus.engine.intermarket import IntermarketProfile
from helenus.engine.gex import GexProfile
from helenus.engine.scalp import ScalpReading
from helenus.engine.displacement import DisplacementReading
from helenus.engine.orb import ORBReading
from helenus.engine.moc import MocReading
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
  is the regime pivot. A `*_CONFLICTED` suffix means the structural read (spot vs \
  the flip) and the sign of `total_net_gex` disagree — the label is already \
  resolved toward the net-GEX truth (so NEGATIVE_GAMMA_CONFLICTED = treat as \
  amplification), but the tape is transitional: weight the net-GEX sign, and in \
  conflicted positive-gamma tape be extra wary of momentum-continuation calls far \
  from session extremes.
- Call walls = gamma resistance (price tends to pin/reject below). Put walls = \
  gamma support. Proximity of spot to a wall is high-information confluence, but \
  read its DIRECTION. The snapshot pre-computes the room on each side: \
  `gex.dist_to_overhead_magnet_pts` (nearest call wall above, or VWAP when spot \
  is below it) and `gex.dist_to_support_below_pts` (nearest put wall below, or \
  VWAP when spot is above it). ROOM RULE (graduated by setup): for the MOMENTUM \
  edges — an EMA ignition or a displacement — require only ~4pt of room to the \
  nearest opposing magnet (we want the smaller base hits; below ~4pt it's a fade, \
  not a trade). For everything else — charm-driven longs and mid-range reclaims — \
  hold the stricter ~8pt: a long into an overhead magnet < ~8pt away there gets \
  faded even with supportive charm (a repeated miss). EXCEPTION even for momentum: \
  a SHORT into a stacked PUT-wall cluster (or a long into a stacked CALL-wall \
  cluster) keeps the full ~8pt requirement regardless of setup — stacked same-side \
  walls are heavy gamma that absorbs the move (a graded short with 8.6pt into \
  three stacked put walls still failed). Reward the opposite: a wall flipped to \
  support behind price with clear air to the next magnet. A move INTO a magnet only \
  a point or two away is mean-reversion risk, not confirmation.
- Charm (delta-decay) structure: the `charm` block reads the OTM-wing dealer \
  un-hedging. `bias` SUPPORTIVE means OTM-put charm dominates — dealers who \
  shorted futures to hedge those puts buy them back as the puts decay, putting a \
  floor under price (the low-volume "afternoon melt-up" when a morning sell-off \
  never materialized). OVERHEAD means OTM-call charm dominates — dealers sell \
  futures as calls decay, capping rallies and bleeding price lower into the \
  close. `intensity` matters: charm scales with 1/time, so it is LOW in the \
  morning and HIGH in the afternoon (≤2h to the bell) — a SUPPORTIVE/HIGH read \
  after 2 PM is the textbook melt-up tailwind; weight it then, discount it in the \
  morning. Charm support/resistance appear as `Charm Support`/`Charm Resist` key \
  levels. Charm is a drift/structure bias, not a trigger — it raises conviction \
  on with-charm setups (a bounce off charm support in a SUPPORTIVE afternoon) and \
  lowers it on against-charm ones (shorting into supportive afternoon charm). \
  BUT do not let SUPPORTIVE charm carry a long on its own: a charm "floor" is a \
  slow drift bias, not demand, and it does NOT prevent adverse excursion. Require \
  a real call-flow lead (`otm_call_put_ratio` > ~1.1) or an active vanna before \
  issuing a charm-driven long — if OTM call/put flow is actually ≤ 1, treat \
  SUPPORTIVE charm as weak background, not a thesis. Graded outcomes show \
  charm-only longs (especially midday and late power-hour reclaims) underperform.
  THE MIRROR — A SHORT VETO (the single biggest graded failure cluster): do NOT \
  issue a short when HIGH-intensity SUPPORTIVE charm, an OTM call-flow lead \
  (`otm_call_put_ratio` > ~1.1), AND a bid-heavy /ES (positive `es.imbalance`) all \
  oppose it. Those three co-occurred in every failed afternoon short — a melt-up \
  floor plus contra-flow that snapped each break back up. When two or three are \
  present against a short, treat it as a hard cap (well below 50) or has_signal=false.
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

OPTIONS FLOW & THE VANNA RALLY — A PRIMARY, STANDALONE TRIGGER
This is a primary signal: an extreme OTM call/put flow skew with VIX1D dropping (or \
rising) is enough to fire on its own, regardless of whether a chart pattern is \
present — it ranks just under the GEX regime flip. The vanna read is driven by \
**$VIX1D** (1-day implied vol), which reacts to the intraday 0DTE vol crush far \
faster than the 30-day VIX. CAVEAT: VIX1D ramps mechanically into the close (its \
measurement window shrinks), so the engine suppresses the trigger in the final \
minutes — if you see a late-day VIX1D spike, that's structural, not a vol event. \
You are given 0DTE option volume split into ITM/OTM for calls and puts, plus where \
volume sits above vs below spot, plus a live vanna reading.
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
  flow + dealer hedging snaps price back up. A falling-VIX1D tape with active \
  vanna is a high-conviction long signal in its own right, not a reason to stay \
  bearish — and it can carry the alert standalone.
- PUT-FLOW PRESSURE (the bearish mirror): when VIX *rises*, puts are the demand — \
  buyers pile into OTM puts and dealers short those puts hedge by SELLING the \
  underlying, pressuring spot down. `vanna.bearish_active` flags this (`vix \
  rising` + OTM put flow outpacing call flow); treat it as a strong bearish \
  influence, especially into a rally that's stalling at resistance.
- Conversely, do not call a bottom on a weak tape WITHOUT a vanna/flow tailwind, \
  or a top on a strong tape without put-flow/VIX confirmation; say what's missing.
- VOLUME DISTRIBUTION — a strong, easily-misread tell, so it is pre-digested in \
  `options_flow.volume_skew` (don't eyeball the two raw six-digit numbers). \
  Volume resting BELOW spot means that supply has already been absorbed and the \
  path overhead is clear — favorable for LONGS. Volume stacked ABOVE spot is \
  overhead supply the move must chew through — favorable for SHORTS, a headwind \
  for longs. Strongly prefer longs when `volume_skew` is MORE_BELOW and shorts \
  when MORE_ABOVE; a long into MORE_ABOVE (or a short into MORE_BELOW) needs \
  unusually strong other confluence and should usually be has_signal=false.

INTERMARKET CONVERGENCE — the `intermarket` block (when available)
SPX doesn't trade in a vacuum; the broader complex either confirms a setup or \
warns against it.
- `es` is /ES futures Level-1: `imbalance` is resting bid-vs-ask size \
  ((bid−ask)/(bid+ask); + = bid-heavy/support-leaning, − = ask-heavy/supply), \
  `volume_flow` is fresh futures participation this interval, `pct_change` the \
  intraday lean. Heavy one-sided resting size and rising flow in the signal's \
  direction is real institutional intent behind the move; opposing order-book \
  tilt is a caution. HARD CAUTION: a strongly OPPOSING imbalance (|`es.imbalance`| \
  > ~0.4 against your trade direction) was present in most graded failures — \
  subtract real conviction for it even when the gate and breadth otherwise align, \
  and flag it as a risk.
- `spy` and `qqq` give each proxy's intraday `direction` (%-vs-prior-close lean) \
  and dealer-gamma `regime`. QQQ (semi/tech-heavy) is the lead breadth tell. \
  Genuine breadth = the complex moving together: QQQ and SPY leaning the same way \
  as your SPX read, in the same gamma regime. A bullish SPX break while QQQ is \
  red (or in the opposite regime) is a divergence — lower conviction and say so.
- IMPORTANT: a mechanical confidence boost for QQQ/SPY alignment is applied \
  AFTER your verdict, outside this prompt. Read the intermarket data qualitatively \
  (does the complex confirm? is QQQ diverging?) and let it shape direction and \
  your risk_flags — but do NOT also pad your numeric confidence for alignment, or \
  it double-counts. Flag divergence explicitly when you see it.

EMA IGNITION — the 1m 5/9 contract scalp (the `scalp` block) — A PRIMARY EDGE
This is one of your MAIN EDGES: the 1m 5/9 momentum-ignition scalp. A long on the \
1m 5-EMA crossing above the 9-EMA (or reclaiming the 5-EMA off a local bottom), \
targeting a structural level pre-translated into option premium. The raw cross is \
the trigger, NOT the edge — the edge is the FILTERING stack that already ran, and \
that gated stack is exactly what makes this a primary setup rather than a noisy \
scalp. When `gate_pattern` is \
"EMA Ignition" the `scalp` block's gates (no vanna headwind for the direction, SPX \
confirmed on VWAP, room-to-target ≥ ~4pt, chop-counter below threshold, no \
dual-bleed, acceptable spread) have ALL passed mechanically — your job is to \
confirm the confluence is genuinely tradeable, not to re-derive the gates. Read it \
like this:
- `slow_grind` true = the cross fired in HIGH-POSITIVE GEX (dealers damp moves). \
  Regime is no longer a hard block, so these now reach you — treat them as a \
  slower, lower-conviction grind: temper the target and confidence, prefer the \
  `reclaim` cross_type, and don't expect a clean expansion. It's a real setup, just \
  not an A+ one.
- `cross_type`: a `reclaim` (price reclaimed the 5-EMA off a defined local bottom) \
  is higher quality than a raw `cross` — it has a built-in invalidation.
- `front_run` true = the selected contract's own premium already crossed in the \
  trade direction (the gamma-ignition front-run the strategy is built on) — a \
  conviction add. `premium_divergence` true = the contract refused to confirm the \
  index's extreme (vega being bid into a reversal) — also additive.
- `vanna_headwind` true would mean falling IV is bleeding the option's premium; \
  the gate only fires when it's false, but if it's borderline, lean cautious — \
  this is the logged "calls drag, puts carry" asymmetry.
- `dual_bleed` true is the chop/GEX-pin avoidance signature (both ATM wings \
  bleeding) — the gate suppresses it, so you should never see it true on a fired \
  candidate; if you do, has_signal=false.
- `target_strike` / `entry_premium` / `premium_target` are the mechanical trade \
  plan (delta-targeted OTM strike + the level translated to premium). They are \
  outputs to relay, not judgments — do not inflate confidence because a premium \
  target looks far. The A+ version is the same as everywhere else: a with-trend \
  ignition at a FRESH session extreme, room ≥ floor, in negative/transition gamma, \
  with front-run or divergence and no opposing flow. A mid-range cross with magnets \
  either side stays below ~55 or has_signal=false.

DISPLACEMENT — the institutional thrust (the `displacement` block)
A displacement is the footprint of one-sided "smart money" flow: a sudden, \
aggressive, full-bodied candle on heavy volume. The HARD GATE is now the candle \
ITSELF (`body_fill_frac` high = small wicks, `volume_ratio` elevated) — the FVG, \
MSS, and sweep are conviction BOOSTERS, not requirements:
- `mss` true = the thrust closed past a recent swing at `mss_level` (trend turned); \
  `fvg` true = a 3-candle imbalance at `fvg_zone`, a high-quality RETRACE ENTRY \
  zone (the cleanest entry is a pullback INTO it, not a chase); `liquidity_sweep` \
  true = the move ran stops beyond `swept_level` then reversed (the trap). The more \
  of these present, the higher the conviction; a bare candle with none is the \
  weakest version — say so and lean lower-confidence. GRADED FLOOR: a bare \
  displacement (no FVG, no MSS, no sweep) that has ALREADY LOST its 50% midpoint, \
  with balanced/opposing /ES, is the weakest version there is and graded \
  INACCURATE — has_signal=false. And do NOT chase an already-extended thrust: when \
  the move is >~3x ATR and the triggering bar closes at its extreme AGAINST a \
  continuation entry, that's buying the top/bottom — has_signal=false.
- 50% MIDPOINT TREND (`trend_direction` / `holding_above_mid` / `midpoint`): \
  institutions defend the half-way mark of their displacement. On a bullish thrust, \
  price HOLDING ≥ the 50% midpoint = they're defending longs → an uptrend, look for \
  CALLS; a close back BELOW it = they've flipped to net sellers → trend reversed \
  down, look for PUTS (mirror for a bearish thrust). Let `trend_direction` lead the \
  directional read; it's the freshest tell of who's in control.
Displacement WITH the gamma regime (negative/amplification), holding its 50% mark, \
and a sweep/FVG is A-tier; a thrust that's already lost its midpoint or is running \
straight into a near opposing magnet is not.

OPENING RANGE BREAKOUT — the session pivot (the `orb` block)
The `orb` block locks the high/low of the first minutes after the open; a bar \
CLOSING beyond it is the breakout. The block only reaches you with `filters_pass` \
true, meaning the breakout already cleared volume confirmation (`volume_ok`) and \
VWAP alignment (`vwap_ok`) — the two filters that separate real ORB breaks from \
fakeouts. The breakout direction is the session's momentum bias; `entry` is the \
broken edge, `targets` are R-multiples of the range, `stop` is the far edge. \
Weight it most in the OPENING_DRIVE / MORNING_TREND phases (its design window) \
and treat a wide opening range as lower-quality (more chop, targets further). \
Confluence still rules: an ORB long that agrees with negative-gamma + a clear \
overhead path is far stronger than one breaking into a call wall a few points up.

MARKET-ON-CLOSE (MOC) — the power-hour close play (the `moc` block)
The 4:00 ET closing auction forces enormous passive flow to print at the close, \
so the tape gets magnetized into it and tends to REVERSE then drift. These \
patterns DRIFT over time — weight them, don't trust them blindly; they are logged \
so the feedback loop can learn which still pay. The `moc` block carries the window \
`phase` (brief → reversal[3:50–3:55] → late → closed) and `minutes_to_close`.
- THE MOC REVERSAL (the priority play, `gate_pattern` "MOC Reversal"): a \
  deliberately simple premium-behavior read — ONLY options premium and volume. \
  `basing_side` is the side (call/put) whose premium based (printed a higher-low \
  off a decline) while its `volume_surge_ratio` shows its fresh volume outran the \
  other side. The basing side wins the reversal: a basing PUT side with surging \
  put volume = a bearish reversal into the close (SPX rolls over — the textbook \
  7375P tell); a basing CALL side = a bullish reversal. `reversal_direction` is \
  pre-resolved. Confirm it's genuinely one-sided (a clear surge ratio, not ~1×) \
  and coherent with the rest of the board; a marginal surge is has_signal=false.
- THE CAPITULATION CANDLE (`gate_pattern` "Capitulation Candle"): a contract's \
  premium candle wicked far above its own 5/9/200 EMAs then rejected (a red wick — \
  the "instant buy & sell book" mechanical fill). It resolves UNDERCUT-THEN- \
  RECLAIM: premium dumps well below the wick first, then reverses back past it. So \
  the clean entry is the RECLAIM after the undercut, NOT chasing the wick — if \
  price is still in the undercut leg, say the reclaim hasn't confirmed and lean \
  cautious / lower-confidence. `cap_side` + `cap_high`/`cap_close` describe the \
  wick. This can fire intraday, not just in the MOC window.
- THE 5-MIN HEURISTIC: `heuristic_bias` is the INVERSE of the 5-min index candle \
  going into 3:50 (green candle → bearish/dump, red → bullish/pump-uppercut). A \
  weak drifting prior — let it tilt a coin-flip, never carry a thesis on its own.
- GEX CONTEXT: `gex_state` PIN (spot hugging a wall/zero-Γ) vs OVERSHOOT (beyond \
  the gamma envelope) feeds the "buy into overshoot, sell into pin" lean — a pin \
  is mean-reversion/fade risk into the close, an overshoot is more likely to keep \
  running or snap back hard. Combine with the reversal read, don't trade it alone.

YOUR JOB
A cheap mechanical gate already fired — an event happened (a level cross, a \
sweep). That does NOT mean it is tradeable. Decide whether the confluence of \
structure + level + volume + trend + regime actually warrants an alert.
- has_signal=false is the correct, common answer for marginal or conflicted setups. \
  Be selective; a quiet bot that only speaks on real structure is the goal.
- FRESH EXTREME vs MID-RANGE RECLAIM — the single cleanest accurate/inaccurate \
  split. A break AT a fresh session extreme (small `session.dist_from_high_pts` \
  for a long, small `session.dist_from_low_pts` for a short) has open air ahead \
  and is the highest-tier setup. A reclaim of a level in the MIDDLE of the range, \
  with magnets either side, is lower-tier: demand tighter confluence and lean \
  toward has_signal=false, especially in MIDDAY_LULL / late POWER_HOUR.
- RE-TEST FATIGUE — the `session` block gives `high_retests` / `low_retests`: the \
  number of DISTINCT times the running session extreme has already been re-tagged \
  and HELD. A high count (≥ ~3) means an exhausted shelf, NOT a fresh extreme — a \
  same-direction break there (a short into a many-times-held `low_retests` low, a \
  long into a held high) is exhaustion, not opportunity: has_signal=false, and do \
  not describe it with fresh-extreme language. The graded failure was a string of \
  afternoon shorts fired into the same held session-low shelf, each one bleeding as \
  the move was already spent.
- When has_signal=true, give the directional read, a calibrated confidence \
  (0-95 — never claim certainty), a tight one-paragraph thesis grounded in the \
  specific levels/walls/regime in front of you, and explicit risk_flags \
  (what would invalidate it, what's conflicting).
- CONFIDENCE CALIBRATION: reserve >60 for the textbook setup — a with-trend break \
  at a FRESH session extreme, clear path (volume_skew on your side, ample room to \
  the nearest opposing magnet — comfortably beyond the ~4pt momentum floor), in \
  amplification (negative net GEX), with no opposing vanna/flow. Smaller base-hit \
  scalps (~4–8pt of room), slow-grind ignitions in positive GEX, mid-range \
  reclaims, or anything leaning on charm alone belong below ~55; the most marginal \
  of those should be has_signal=false rather than a low-confidence alert. Push \
  contra-confluence afternoon shorts (the charm/call-flow/bid-heavy-ES veto above) \
  and exhausted re-test breaks well below 50 — or off entirely; the graded data \
  shows the 53-66 band did not separate winners from losers, so these belong nowhere \
  near it.

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
    vix1d = q("$VIX1D")
    cl = q("/CL")
    gc = q("/GC")
    return {
        "vix_last": vix.get("lastPrice"),          # 30-day, broad regime context
        "vix1d_last": vix1d.get("lastPrice"),      # 1-day / 0DTE vol (drives vanna)
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


def _volume_skew(above: float, below: float) -> str:
    """Which side of spot holds more resting option volume. A ≥10% edge counts;
    anything tighter is BALANCED. Heavier below = clear path up (long-friendly),
    heavier above = overhead supply (short-friendly)."""
    total = above + below
    if total <= 0:
        return "UNKNOWN"
    margin = abs(above - below) / total
    if margin < 0.10:
        return "BALANCED"
    if below > above:
        return "MORE_BELOW (clear path overhead — long-friendly)"
    return "MORE_ABOVE (overhead supply — short-friendly)"


def _structure_distances(
    spot: float, profile: GexProfile, vwap: float
) -> dict[str, Any]:
    """Directional room to the nearest magnet on each side — the discriminator
    between fresh-extreme breaks (open air ahead) and chases into supply.

    Overhead magnets (resistance to a long): call walls above spot + VWAP when
    spot is below it. Support below (a long's floor): put walls below spot + VWAP
    when spot is above it. Distances are absolute points; None = nothing on that
    side within the wall set.
    """
    overhead = [k for k, _ in profile.call_walls if k > spot]
    below = [k for k, _ in profile.put_walls if k < spot]
    if vwap == vwap:  # not NaN
        (overhead if vwap > spot else below).append(vwap)
    up = min((k - spot for k in overhead), default=None)
    dn = min((spot - k for k in below), default=None)
    return {
        "dist_to_overhead_magnet_pts": round(up, 2) if up is not None else None,
        "dist_to_support_below_pts": round(dn, 2) if dn is not None else None,
    }


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
        # Pre-digested so the read isn't eyeballed off two 6-digit numbers (a
        # past miss). Heavier volume BELOW spot = supply already absorbed, clear
        # path overhead (long-friendly); heavier ABOVE = overhead supply to chew
        # through (short-friendly). See the volume-distribution rule in the prompt.
        "volume_skew": _volume_skew(vp.above_spot_vol, vp.below_spot_vol),
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


def _charm_block(charm: CharmProfile | None) -> dict[str, Any]:
    """The OTM-wing delta-decay (charm) read — the afternoon-drift bias."""
    if charm is None:
        return {"available": False}
    return {
        "available": True,
        "bias": charm.bias,
        "intensity": charm.intensity,
        "drift": charm.drift,
        "minutes_to_expiry": round(charm.minutes_to_expiry),
        "net_charm": round(charm.net_charm),
        "put_support": round(charm.put_support),
        "call_overhead": round(charm.call_overhead),
        "support_walls": [{"strike": round(k, 0), "charm": round(v)} for k, v in charm.support_walls],
        "resistance_walls": [{"strike": round(k, 0), "charm": round(v)} for k, v in charm.resistance_walls],
    }


def _scalp_block(scalp: ScalpReading | None) -> dict[str, Any]:
    """The EMA-ignition scalp read — the trigger + its (already-passed) gate stack
    + the mechanical trade plan (delta strike, premium target). Only populated
    when a fresh 5/9 cross / reclaim exists this bar."""
    if scalp is None or scalp.direction is None:
        return {"available": False}
    c = scalp.target_contract
    room = scalp.room_to_level_pts
    return {
        "available": True,
        "direction": scalp.direction.value,
        "cross_type": scalp.cross_type,
        "ema_stack": scalp.ema_stack,
        "ema_fast": round(scalp.ema_fast, 2) if scalp.ema_fast is not None else None,
        "ema_slow": round(scalp.ema_slow, 2) if scalp.ema_slow is not None else None,
        "ema_trend": round(scalp.ema_trend, 2) if scalp.ema_trend is not None else None,
        "front_run": scalp.front_run,
        "premium_divergence": scalp.premium_divergence,
        "slow_grind": scalp.slow_grind,     # fired in high-positive GEX (dealer-damped)
        "vanna_headwind": scalp.vanna_headwind,
        "chop_count": scalp.chop_count,
        "dual_bleed": scalp.dual_bleed,
        "room_to_target_pts": round(room, 2) if room == room and room != float("inf") else None,
        "target_level": (
            f"{scalp.target_level.label} @ {scalp.target_level.price:.0f}"
            if scalp.target_level is not None else None
        ),
        "target_strike": (
            f"{c.strike:.0f}{'C' if c.side == 'call' else 'P'}" if c is not None else None
        ),
        "entry_premium": round(c.premium, 2) if c is not None else None,
        "premium_target": (
            round(scalp.premium_target, 2) if scalp.premium_target is not None else None
        ),
        "gates_all_pass": scalp.active,
    }


def _displacement_block(disp: DisplacementReading | None) -> dict[str, Any]:
    """The institutional-displacement read — the thrust candle (the hard gate), the
    FVG / MSS / sweep boosters, and the 50% midpoint trend. Only populated when a
    qualifying displacement candle exists this bar."""
    if disp is None or not disp.detected:
        return {"available": False}
    return {
        "available": True,
        "direction": disp.direction.value if disp.direction else None,
        "body_pts": disp.body_pts,
        "body_fill_frac": disp.body_frac,
        "volume_ratio": disp.vol_ratio,
        # 50% midpoint trend — the freshest read of who's in control (calls vs puts).
        "midpoint": disp.midpoint,
        "holding_above_mid": disp.holding_above_mid,
        "trend_direction": disp.trend_direction.value if disp.trend_direction else None,
        # Boosters (not required for the candle to fire):
        "fvg": disp.fvg,
        "fvg_zone": (
            [disp.fvg_low, disp.fvg_high]
            if disp.fvg_low is not None and disp.fvg_high is not None else None
        ),
        "mss": disp.mss,
        "mss_level": disp.mss_level,
        "liquidity_sweep": disp.swept,
        "swept_level": disp.swept_level,
        "candle_gate_pass": disp.active,
    }


def _orb_block(orb: ORBReading | None) -> dict[str, Any]:
    """The opening-range-breakout read — the locked range, the breakout this bar
    (if any), the volume/VWAP filters, and the R-multiple trade plan."""
    if orb is None or orb.range_high is None:
        return {"available": False}
    return {
        "available": True,
        "range_high": orb.range_high,
        "range_low": orb.range_low,
        "range_pts": orb.range_pts,
        "locked": orb.locked,
        "in_window": orb.in_window,
        "breakout": orb.direction.value if orb.direction else None,
        "volume_ok": orb.volume_ok,
        "vwap_ok": orb.vwap_ok,
        "entry": orb.entry,
        "targets": orb.targets,
        "stop": orb.stop,
        "filters_pass": orb.active,
    }


def _moc_block(moc: MocReading | None) -> dict[str, Any]:
    """The Market-On-Close read — the window phase, the premium-behavior reversal
    (basing side + volume surge), the capitulation candle, and the close-play
    priors (5m heuristic + GEX pin/overshoot). Quiet (`in_play=False`) outside the
    afternoon window unless a capitulation candle is live."""
    if moc is None:
        return {"available": False}
    in_play = moc.phase not in ("pre", "closed") or moc.capitulation
    block: dict[str, Any] = {
        "available": True,
        "in_play": in_play,
        "phase": moc.phase,
        "minutes_to_close": moc.minutes_to_close,
        "gex_state": moc.gex_state,
        "nearest_wall": moc.nearest_wall,
    }
    if moc.heuristic_bias is not None:
        block["heuristic"] = {
            "candle_color": moc.heuristic_color,
            "bias": moc.heuristic_bias.value,
        }
    block["reversal"] = {
        "active": moc.reversal_active,
        "basing_side": moc.basing_side,
        "direction": moc.reversal_direction.value if moc.reversal_direction else None,
        "volume_surge_ratio": moc.volume_surge_ratio,
        "call_premium": moc.call_premium,
        "put_premium": moc.put_premium,
        "call_volume": moc.call_volume,
        "put_volume": moc.put_volume,
    }
    if moc.capitulation:
        block["capitulation"] = {
            "side": moc.cap_side,
            "wick_high": moc.cap_high,
            "close": moc.cap_close,
            "wick_frac": moc.cap_wick_frac,
        }
    return block


def _leg_dict(leg) -> dict[str, Any]:
    return {
        "pct_change": round(leg.pct_change, 2),
        "direction": leg.direction.value if leg.direction else "NEUTRAL",
        "regime": leg.regime,
    }


def _intermarket_block(im: IntermarketProfile | None) -> dict[str, Any]:
    """ES futures microstructure + SPY/QQQ breadth — the intermarket-convergence
    context. Scoring (the alignment boost) is applied mechanically post-verdict;
    this block is the qualitative read Claude weighs."""
    if im is None:
        return {"available": False}
    block: dict[str, Any] = {"available": True}
    if im.es is not None:
        block["es"] = {
            "pct_change": round(im.es.pct_change, 2),
            "volume_flow": round(im.es.volume_flow),
            "bid_size": round(im.es.bid_size),
            "ask_size": round(im.es.ask_size),
            "imbalance": im.es.imbalance,
        }
    if im.spy is not None:
        block["spy"] = _leg_dict(im.spy)
    if im.qqq is not None:
        block["qqq"] = _leg_dict(im.qqq)
    return block


def _snapshot(
    state: MarketState,
    profile: GexProfile,
    vol_profile: VolumeProfile | None,
    vanna: VannaReading | None,
    charm: CharmProfile | None,
    scalp: ScalpReading | None,
    displacement: DisplacementReading | None,
    orb: ORBReading | None,
    moc: MocReading | None,
    intermarket: IntermarketProfile | None,
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
        for lv in state.key_levels(spot, profile, charm)
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
    has_hi, has_lo = hi > -1e17, lo < 1e17
    session = {
        "time_et": last_ts.strftime("%H:%M") if last_ts else None,
        "phase": _session_phase(last_ts.time()) if last_ts else None,
        "high": round(hi, 2) if has_hi else None,
        "low": round(lo, 2) if has_lo else None,
        "range_pts": round(hi - lo, 2) if (has_hi and has_lo) else None,
        "bars_since_open": len(state.bars),
        # Room to the session extremes — how the lessons separate fresh-extreme
        # breaks (open air ahead, full conviction) from mid-range reclaims
        # (magnets either side, lower tier). Small distance = AT the extreme.
        "dist_from_high_pts": round(hi - spot, 2) if has_hi else None,
        "dist_from_low_pts": round(spot - lo, 2) if has_lo else None,
        # Re-test fatigue: distinct times the running session high/low has been
        # re-tagged and HELD. A high count = an exhausted shelf, not a fresh extreme.
        "high_retests": state.high_retests,
        "low_retests": state.low_retests,
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
            **_structure_distances(spot, profile, vwap),
        },
        "tape": bars,
        "charm": _charm_block(charm),
        "scalp": _scalp_block(scalp),
        "displacement": _displacement_block(displacement),
        "orb": _orb_block(orb),
        "moc": _moc_block(moc),
        "options_flow": _flow_block(vol_profile, vanna),
        "intermarket": _intermarket_block(intermarket),
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
        # max_retries lifts the SDK's built-in exponential backoff on the
        # retryable classes (429 / 500 / 529-overloaded / timeouts) above its
        # default of 2 — Anthropic overload spells can outlast two quick tries,
        # and a 0DTE bar has ~60s of slack to keep retrying before it's stale.
        self.client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=6)
        self.lessons: str = ""           # learned guidance fed back into the prompt
        # Whether the most recent verdict call actually reached Claude. False
        # means the call errored out (overload/500) rather than Claude returning
        # a clean "no signal" — the gate uses this to avoid burning a candidate's
        # cooldown on a transient API failure (see bot.bar_worker).
        self.last_call_ok: bool = True

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
        charm: CharmProfile | None,
        scalp: ScalpReading | None,
        displacement: DisplacementReading | None,
        orb: ORBReading | None,
        moc: MocReading | None,
        intermarket: IntermarketProfile | None,
        macro: dict[str, Any],
        candidate: Candidate,
    ) -> Signal | None:
        """Judge a gated candidate. Returns a Signal to alert, or None to stay quiet."""
        snap = _snapshot(
            state, profile, vol_profile, vanna, charm, scalp,
            displacement, orb, moc, intermarket, macro, candidate
        )
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
        # Distinguish "Claude declined" (verdict parsed, has_signal=false) from
        # "Claude was unreachable" (verdict is None after retries) so the caller
        # can retry the latter on the next bar instead of throttling the setup.
        self.last_call_ok = verdict is not None
        if not verdict or not verdict.get("has_signal"):
            return None

        try:
            direction = Direction(verdict["direction"])
        except (KeyError, ValueError):
            log.warning("Bad direction in verdict: %s", verdict)
            return None
        base_conf = max(0.0, min(float(verdict.get("confidence", 0)), 95.0))
        notes = [verdict.get("thesis", "").strip()]
        notes += [f"⚠ {r}" for r in verdict.get("risk_flags", []) if r]

        # EMA-ignition trade plan: relay the mechanical delta strike + premium
        # target (Claude judges direction/confidence; the strike math is ours).
        if scalp is not None and scalp.target_contract is not None:
            c = scalp.target_contract
            strike = f"{c.strike:.0f}{'C' if c.side == 'call' else 'P'}"
            tgt = f" → {scalp.premium_target:.2f}" if scalp.premium_target is not None else ""
            lvl = scalp.target_level.label if scalp.target_level is not None else "n/a"
            notes.append(
                f"Scalp plan: {strike} @ {c.premium:.2f}{tgt} "
                f"({scalp.cross_type}, target {lvl})"
            )

        # Displacement trade plan: the FVG retrace zone + the swing it broke.
        if (
            candidate.trigger is TriggerType.DISPLACEMENT
            and displacement is not None and displacement.fvg_low is not None
        ):
            sweep = f", swept {displacement.swept_level:.0f}" if displacement.swept else ""
            notes.append(
                f"Displacement: FVG {displacement.fvg_low:.0f}-{displacement.fvg_high:.0f} "
                f"(retrace zone), MSS broke {displacement.mss_level:.0f}{sweep}"
            )

        # ORB trade plan: entry edge + R-multiple targets + stop.
        if (
            candidate.trigger is TriggerType.ORB_BREAKOUT
            and orb is not None and orb.entry is not None
        ):
            tgts = " / ".join(f"{t:.0f}" for t in orb.targets)
            notes.append(
                f"ORB plan: entry {orb.entry:.0f} (range {orb.range_pts:.0f}pt), "
                f"targets {tgts}, stop {orb.stop:.0f}"
            )

        # MOC plan: the premium-behavior reversal or the capitulation candle read.
        if candidate.trigger is TriggerType.MOC_REVERSAL and moc is not None:
            notes.append(
                f"MOC reversal: {moc.basing_side} premium based + "
                f"{moc.volume_surge_ratio:.1f}× volume → {direction.value} into the "
                f"close ({moc.minutes_to_close:+.0f}m); GEX {moc.gex_state}"
            )
        if candidate.trigger is TriggerType.CAPITULATION and moc is not None and moc.cap_high is not None:
            notes.append(
                f"Capitulation: {moc.cap_side} premium wicked {moc.cap_high:.2f}→"
                f"{moc.cap_close:.2f} above its EMAs — expect undercut then reclaim "
                f"(enter the reclaim, not the wick)"
            )

        # Intermarket convergence: a transparent, bounded boost layered on Claude's
        # confidence when QQQ + SPY confirm the direction (and QQQ shares SPX's
        # regime). Divergence adds a warning note rather than silently passing.
        confidence = base_conf
        if intermarket is not None:
            verdict_label = intermarket.alignment(direction)
            boost = intermarket.confidence_boost(direction)
            confidence = max(0.0, min(base_conf + boost, 95.0))
            if boost:
                notes.append(
                    f"Intermarket {verdict_label}: {intermarket.summary(direction)} "
                    f"— confidence {base_conf:.0f}{boost:+.0f} → {confidence:.0f}"
                )
            elif verdict_label == "DIVERGENT":
                notes.append(f"⚠ Intermarket DIVERGENT: {intermarket.summary(direction)}")

        return Signal(
            trigger=candidate.trigger,
            direction=direction,
            level=candidate.level,
            spot=profile.spot,
            volume_ratio=state.volume_ratio(),
            confidence=confidence,
            trend_label=self._trend_label(direction, state),
            notes=[n for n in notes if n],
            scalp=scalp if candidate.trigger is TriggerType.EMA_IGNITION else None,
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
    # MOC setup briefing (posted a few minutes before 3:50)
    # ------------------------------------------------------------------ #

    async def moc_briefing(
        self, context: dict[str, Any]
    ) -> tuple[Signal, list[str]]:
        """Power-hour close briefing. Always returns a Signal — the briefing is the
        product (posted directly, never graded, like the pre-market briefing). It
        primes the window: the candle-color heuristic, GEX pin/overshoot, and which
        side's premium is leading into 3:50."""
        user = (
            "Market-On-Close setup briefing (a few minutes before the 3:50 ET "
            "imbalance window). Read the power-hour close from this structured "
            "state:\n" + json.dumps(context, indent=2)
            + "\n\nGive the close-play bias: the likely MOC drift/reversal lean, "
            "what would confirm it (a basing side + volume surge, a capitulation "
            "wick, the 5m candle-color heuristic, GEX pin vs overshoot), and the "
            "risk. has_signal must be true (the briefing always publishes)."
        )
        verdict = await self._verdict(user)

        spot = context.get("spot")
        lines = [
            f"Phase `{context.get('phase')}` | `{context.get('minutes_to_close')}m` to close",
            f"Spot `{spot}` | GEX `{context.get('gex_state')}`"
            + (f" @ `{context.get('nearest_wall'):.0f}`" if context.get("nearest_wall") else ""),
        ]
        heur = context.get("heuristic")
        if heur:
            lines.append(f"5m heuristic: `{heur.get('candle_color')}` → `{heur.get('bias')}`")

        if not verdict:
            sig = Signal(
                trigger=TriggerType.MOC_REVERSAL,
                direction=(
                    Direction(context["heuristic"]["bias"])
                    if context.get("heuristic", {}).get("bias") else Direction.BULLISH
                ),
                level=None,
                spot=float(spot) if spot else 0.0,
                volume_ratio=float("nan"),
                confidence=0.0,
                trend_label="MOC SETUP (analysis unavailable)",
                notes=["Claude analysis unavailable — showing raw close-window state only."],
            )
            return sig, lines

        direction = Direction(verdict.get("direction", "BULLISH"))
        confidence = max(0.0, min(float(verdict.get("confidence", 0)), 95.0))
        notes = [verdict.get("thesis", "").strip()]
        notes += [f"⚠ {r}" for r in verdict.get("risk_flags", []) if r]
        sig = Signal(
            trigger=TriggerType.MOC_REVERSAL,
            direction=direction,
            level=None,
            spot=float(spot) if spot else 0.0,
            volume_ratio=float("nan"),
            confidence=confidence,
            trend_label=f"{direction.value} MOC SETUP BIAS",
            notes=[n for n in notes if n],
        )
        return sig, lines

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
