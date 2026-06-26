"""
Helenus configuration.

All secrets come from the environment (.env supported via python-dotenv).
Everything tunable about the engines lives here so the math modules stay pure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Credentials / endpoints
# ---------------------------------------------------------------------------

DISCORD_TOKEN: str = os.environ.get("HELENUS_DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID: int = int(os.environ.get("HELENUS_CHANNEL_ID", "0"))

SCHWAB_API_KEY: str = os.environ.get("SCHWAB_API_KEY", "")
SCHWAB_APP_SECRET: str = os.environ.get("SCHWAB_APP_SECRET", "")
SCHWAB_CALLBACK_URL: str = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
SCHWAB_TOKEN_PATH: str = os.environ.get("SCHWAB_TOKEN_PATH", "schwab_token.json")

# Claude API — the analysis brain. AsyncAnthropic also reads ANTHROPIC_API_KEY
# from the environment, but we pass it explicitly so a missing key fails loudly.
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

# The Schwab chains endpoint only accepts the underlying root "$SPX" — "$SPXW"
# is rejected with 400 (Invalid Parameter). Pinning from_date == to_date ==
# today is what isolates the 0DTE set: on any non-monthly-expiry day the only
# contracts returned for today are the PM-settled SPXW weeklys (verified: the
# returned contract symbols are "SPXW <yymmdd>C/P<strike>"). See the monthly-
# expiry caveat in fetch_0dte_chain.
CHAIN_SYMBOL: str = "$SPX"
UNDERLYING_SYMBOL: str = "$SPX"

# Schwab returns contract symbols like "SPXW  260622C07520000". On the monthly
# expiry (3rd Friday) the $SPX chain for that date also contains the AM-settled
# monthly (root "SPX  "); we keep only this PM-settled root so GEX and flow
# never mix the two settlement regimes. On every other day it's a no-op.
CHAIN_CONTRACT_ROOT: str = "SPXW"
PROXY_SYMBOL: str = "SPY"          # volume proxy — the index itself prints no volume

# Intermarket complex — corroborate an SPX signal against the broader tape.
# QQQ/SPY get their own 0DTE chains (intermarket_worker); /ES adds futures
# Level-1 microstructure (volume flow + resting bid/ask size). SPY is already
# PROXY_SYMBOL, so it's not duplicated in the quote batch.
QQQ_SYMBOL: str = "QQQ"
ES_SYMBOL: str = "/ES"

# Cboe's 1-day volatility index — implied vol of SPX options expiring TODAY. The
# 0DTE-native vol gauge, far more sensitive to intraday option crushes than the
# 30-day $VIX, so it drives the vanna tracker (see FlowConfig / VannaTracker).
VIX1D_SYMBOL: str = "$VIX1D"

# /ES and QQQ ride the existing batched macro quote call (one extra symbol each,
# negligible cost) so % change + ES Level-1 refresh on the 30s macro cadence.
# $VIX (30-day) stays for the macro board + premarket band; $VIX1D feeds vanna.
MACRO_SYMBOLS: tuple[str, ...] = ("/CL", "/GC", "$VIX", VIX1D_SYMBOL, ES_SYMBOL, QQQ_SYMBOL)

# ---------------------------------------------------------------------------
# Engine tuning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GexConfig:
    strike_count: int = 60              # strikes each side of spot to ingest
    contract_multiplier: int = 100
    wall_top_n: int = 3                 # how many gamma walls to surface per side
    min_oi: int = 10                    # drop dust strikes before aggregation


@dataclass(frozen=True)
class ScanConfig:
    volume_ma_periods: int = 20
    volume_trigger_ratio: float = 2.0   # Trigger 1 threshold
    min_baseline_bars: int = 5          # min priors before a volume ratio computes
    round_number_step: float = 10.0     # SPX round-number grid (7430, 7440, ...)
    major_round_step: float = 50.0      # heavier weight grid (7400, 7450, ...)
    level_proximity_pts: float = 3.0    # "at a level" tolerance in index points
    sweep_pierce_pts: float = 2.0       # Trigger 2: minimum pierce depth
    # Level-rejection trigger: only weighty levels (session extremes, prior
    # close, GEX walls, zero-Γ ≥ 0.7) and a wick at least this deep count, so
    # the touch-and-reverse gate ignores midday noise around minor round numbers.
    rejection_min_weight: float = 0.7
    rejection_wick_pts: float = 3.0
    # A rejection only counts at the EDGE of the established range — the level
    # must sit within this many points of the running session extreme on the
    # relevant side. This is what separates a real range-edge reversal from
    # price merely pinning an interior GEX wall it hovers on all session.
    edge_proximity_pts: float = 10.0
    # Range-expansion (momentum) trigger: fire when net displacement over the
    # lookback exceeds `expansion_atr_mult` × mean bar range AND an absolute floor
    # — a directional thrust between levels that the level-based triggers miss.
    expansion_lookback_bars: int = 5
    expansion_atr_mult: float = 3.0
    expansion_min_pts: float = 10.0     # absolute floor so range-chop doesn't trip it
    bar_seconds: int = 60               # sequential interval size
    gex_cluster_proximity_pts: float = 10.0  # confluence credit distance


@dataclass(frozen=True)
class CharmConfig:
    """Dealer charm (delta-decay) structure — the afternoon-melt-up mechanic.

    Charm is ∂Δ/∂t: how an option's delta bleeds as time passes. Only OTM
    contracts carry the structural story — as their delta decays toward zero,
    dealers who hedged them must un-hedge, mechanically buying/selling the
    underlying. OTM puts → dealers bought back shorts → SUPPORT (positive charm);
    OTM calls → dealers sell longs → OVERHEAD (negative charm). Magnitude scales
    with OI, and because charm ∝ 1/T it intensifies into the afternoon."""
    min_oi: int = 10                    # drop dust strikes before aggregation
    wall_top_n: int = 3                 # charm support/resistance levels per side
    contract_multiplier: int = 100
    # Floor on minutes-to-expiry so the 1/T term stays finite into the bell.
    min_minutes_floor: float = 2.0
    settle_hour: int = 16               # SPXW PM settlement reference (16:00 ET)
    settle_minute: int = 0
    # net put-support vs call-overhead must clear this ratio to call a bias.
    dominance_ratio: float = 1.5
    # Charm support/resistance walls injected as key levels for confluence.
    wall_weight: float = 0.8


@dataclass(frozen=True)
class ThrottleConfig:
    """Adaptive polling — tight during action, relaxed when nothing is moving."""
    base_chain_secs: float = 15.0       # chain poll during regular hours
    slow_chain_secs: float = 60.0       # low-volatility / lunch-chop cadence
    premarket_chain_secs: float = 120.0
    macro_secs: float = 30.0
    # If 5-min realized range on spot falls below this many points, back off.
    low_vol_range_pts: float = 4.0
    # Hard floor between any two Schwab calls (rate-limit safety).
    min_call_gap_secs: float = 0.6


@dataclass(frozen=True)
class FlowConfig:
    """Options-volume tracking and the vanna-rally detector (a primary trigger).

    Driven by $VIX1D (1-day implied vol), not the 30-day $VIX — VIX1D reacts to the
    intraday 0DTE vol crush the vanna rally is built on. The trade-off is VIX1D's
    end-of-day distortion: its measurement window mechanically shrinks into the
    close, so it ramps near 4:00 ET for structural reasons, not real vol. The vanna
    trigger is suppressed inside that window (see vix1d_distort_* / VannaTracker)."""
    # VIX trend: compare the latest sample to one ~this many macro-samples ago.
    # macro_worker runs every 30s, so 6 samples ≈ 3 minutes.
    vix_lookback_samples: int = 6
    vix_drop_pts: float = 0.30          # VIX1D must fall at least this over lookback
    # Fresh OTM-call contracts (interval delta) needed before flow "counts".
    min_call_flow: float = 2000.0
    # OTM call flow must outpace OTM put flow by this multiple to be vanna-active.
    call_dominance_ratio: float = 1.5
    # VIX1D end-of-day distortion window (ET): from this time to the close, the vanna
    # trigger is suppressed so the mechanical late-day ramp isn't read as signal.
    vix1d_distort_hour: int = 15
    vix1d_distort_minute: int = 45
    # --- Flow INFLECTION (the early arm) -------------------------------------
    # The vanna/put-flow level triggers above (vix_drop_pts / min_call_flow) fire
    # only once flow is already large — late by construction. The inflection arm
    # fires earlier off the DERIVATIVE: the rate of OTM-flow accumulation
    # accelerating AND the VIX1D slope rolling over. The level triggers stay as the
    # confirm/fallback. Default OFF so current behavior is exactly preserved. See
    # helenus/engine/flow.py:VannaTracker and scan2.detect_candidate (FLOW_INFLECTION).
    inflection_enabled: bool = False
    inflection_accel_mult: float = 2.0    # latest interval flow vs the prior-interval mean
    inflection_min_flow: float = 800.0    # floor so dead-tape noise can't trip it (< min_call_flow)
    inflection_vix_slope: float = 0.10    # VIX1D pts/interval that counts as a real slope turn
    inflection_flow_lookback: int = 4     # prior intervals averaged for the acceleration baseline


@dataclass(frozen=True)
class ScalpConfig:
    """The 1m 5/9 EMA momentum-ignition scalp — Helenus's contract-scalp confluence.

    Replicates a visual 0DTE strategy: go long when the contract's 1m 5 EMA
    crosses above the 9 EMA (or reclaims the 5 EMA off a local bottom), targeting
    a structural level pre-translated into option premium. The raw cross is noisy,
    so it is treated as the *trigger* and wrapped in a gated confirmation stack
    (regime, vanna headwind, SPX confirmation, chop counter, room-to-level). EMAs
    run on the clean SPX index tape; the selected contract's premium is tracked
    separately for the gamma-ignition front-run and dual-bleed reads.
    See helenus/engine/scalp.py."""
    ema_fast: int = 5
    ema_slow: int = 9
    ema_trend: int = 200                # dynamic floor/ceiling EMA
    # OTM strike selection: target |delta| and the acceptable window around it.
    target_delta: float = 0.35
    delta_band: float = 0.15
    # Chop counter — N+ 5/9 crosses inside the window is a chop signature: stand down.
    chop_window_min: int = 5
    chop_max_crosses: int = 3
    # Room to the nearest structural target in the trade direction (index points).
    # Lowered from 8 → 4 so smaller "base hit" momentum scalps aren't dropped; the
    # analyst's overhead-magnet rule is relaxed to ~4pt for momentum edges to match.
    min_room_pts: float = 4.0
    # Contract bid/ask spread ceiling as a fraction of mid — wider = untradeable.
    max_spread_pct: float = 0.10
    # VIX move (over the vanna lookback) that makes a direction's premium a headwind:
    # falling VIX bleeds call vega (long-call headwind); rising VIX pumps put vega
    # (long-put tailwind). The per-direction premium-quality axis, distinct from the
    # vanna *rally* (a spot-direction signal).
    vix_headwind_pts: float = 0.30
    # Lower-high/lower-low lookback for the dual-bleed (both ATM wings bleeding) check.
    dual_bleed_lookback: int = 5
    premium_history: int = 30           # chain-mark samples kept per tracked contract
    reclaim_lookback_bars: int = 5      # window defining the "local bottom" for a reclaim
    # Second-order (gamma) term in the level->premium translation. We have gamma,
    # so include it for the to-the-penny premium target the strategy wants.
    use_gamma_in_translation: bool = True


@dataclass(frozen=True)
class DisplacementConfig:
    """Institutional displacement — the footprint of one-sided 'smart money' flow.

    A sudden aggressive thrust of large, full-bodied, small-wicked candles. To be
    tradeable it must clear three structural pillars: a market structure shift
    (the move closes past a recent swing), a fair value gap (a 3-candle imbalance
    where candle 1 and candle 3 wicks don't overlap), and ideally a liquidity
    sweep (a stop-hunt pierce of a key level just before the move). The candle +
    FVG + MSS are the hard gate; the sweep is a conviction booster (flip
    `require_sweep` to harden it after tuning). See helenus/engine/displacement.py."""
    body_atr_mult: float = 1.5          # displacement body ≥ this × ATR
    body_frac: float = 0.6              # body / full range (small wicks)
    vol_ratio: float = 1.5              # displacement volume vs 20-bar baseline
    mss_lookback: int = 10              # bars defining the swing the move must break
    sweep_lookback: int = 3             # bars before the move scanned for a sweep
    sweep_pierce_pts: float = 1.0       # how far beyond a level a sweep must pierce
    min_fvg_pts: float = 0.5            # minimum imbalance width to count as an FVG
    # The high-volume displacement CANDLE is the only hard gate; FVG, MSS, and the
    # liquidity sweep are conviction boosters. Flip a require_* flag to harden that
    # pillar back into the gate once its parameters are tuned.
    require_fvg: bool = False
    require_mss: bool = False
    require_sweep: bool = False
    # The 50%-of-range midpoint of the displacement candle: price holding at/above it
    # (bullish thrust) = institutions defending longs → calls; a close back below it
    # = they've flipped to sellers → puts (mirror for a bearish thrust).
    mid_fraction: float = 0.5


@dataclass(frozen=True)
class ORBConfig:
    """Opening Range Breakout — the first-N-minute range as the session's pivot.

    Lock the absolute high/low of the opening window after the 09:30 ET open; a
    1-minute bar CLOSING beyond the locked range is a breakout (long above the
    high, short below the low). Fakeouts are filtered with two hard gates —
    volume confirmation (institutional participation) and VWAP alignment (the
    breakout must agree with the day's volume-weighted trend). Targets are
    R-multiples of the range height. See helenus/engine/orb.py."""
    window_min: int = 15                # opening-range window (5 / 15 / 30 typical)
    vol_confirm_ratio: float = 1.5      # breakout volume vs 20-bar baseline
    require_vwap: bool = True           # skip a long below VWAP / a short above it
    r_multiples: tuple[float, ...] = (1.0, 2.0)   # profit targets in range-heights
    open_hour: int = 9                  # RTH open (ET)
    open_minute: int = 30


@dataclass(frozen=True)
class MocConfig:
    """Market-On-Close — the power-hour close play (~3:50–4:00 ET).

    The closing auction forces huge passive flow to trade at the print, and the
    tape tends to reverse and drift into it in recurring — but DRIFTING — patterns,
    so the engine logs them for the review→lessons loop to learn which still pay.
    Built from mechanical proxies (no imbalance feed): a simplified premium-behavior
    REVERSAL (one side's premium bases while its volume surges vs the other → reversal
    that way), a CAPITULATION candle (a contract's premium wicks above its own 5/9/200
    EMAs then rejects → undercut-then-reclaim), the inverse 5m candle-color heuristic
    (green into 3:50 → dump, red → pump), and GEX pin/overshoot context.
    See helenus/engine/moc.py."""
    # Window timing (ET). A setup briefing posts a few minutes before 3:50; the
    # reversal play is only live in [window_open, reversal_deadline] (3:50–3:55).
    brief_hour: int = 15
    brief_minute: int = 47
    window_open_hour: int = 15
    window_open_minute: int = 50
    reversal_deadline_hour: int = 15
    reversal_deadline_minute: int = 55
    close_hour: int = 16
    close_minute: int = 0
    # The candle-color heuristic reads the N-minute index candle ending at window_open.
    heuristic_candle_min: int = 5
    # Reversal (simplified premium-behavior — ONLY premium + volume): one side's
    # premium prints a higher-low off a decline (bases) while that side's fresh
    # interval volume outpaces the opposing side by `volume_surge_ratio`.
    premium_history: int = 30            # premium samples kept per side
    base_lookback: int = 6               # samples defining a "basing" higher-low
    volume_surge_ratio: float = 1.5      # winning-side interval vol vs opposing side
    min_side_volume: float = 200.0       # interval-volume floor so dead tape stays quiet
    # Capitulation candle: a side's 1m premium candle wicks above its own EMAs then
    # rejects (the "instant buy & sell book" mechanical order). Fires intraday too.
    ema_fast: int = 5
    ema_slow: int = 9
    ema_trend: int = 200
    cap_wick_frac: float = 0.5           # (high-close)/(high-low) ≥ this = rejection wick
    cap_ema_margin: float = 0.05         # high must clear the max mature EMA by this fraction
    # GEX context: spot within this of a wall / zero-Γ reads as a PIN, beyond as OVERSHOOT.
    pin_proximity_pts: float = 3.0


@dataclass(frozen=True)
class AnalystConfig:
    """Tuning for the Claude analysis layer."""
    model: str = "claude-opus-4-8"
    max_tokens: int = 4096              # output cap (includes adaptive thinking)
    # Gate threshold: a level cross only becomes a Claude candidate above this
    # volume multiple. Deliberately looser than the old 2.0x hard trigger —
    # Claude is the judge now, so we let it see marginal events and decide.
    gate_volume_ratio: float = 1.5
    recent_bars: int = 12               # bars of OHLCV tape handed to Claude


@dataclass(frozen=True)
class IntermarketConfig:
    """Intermarket convergence — does the broader complex confirm an SPX signal?

    SPY + QQQ 0DTE gamma structure (their own chains) plus /ES Level-1
    microstructure are read against the SPX setup. When QQQ's intraday direction
    AND gamma regime agree with the signal and with SPY, conviction is boosted
    mechanically after Claude's verdict; QQQ opposing the signal is flagged as a
    divergence warning. See helenus/engine/intermarket.py."""
    spy_symbol: str = PROXY_SYMBOL
    qqq_symbol: str = QQQ_SYMBOL
    es_symbol: str = ES_SYMBOL
    # SPY/QQQ chains poll slower than the 15s SPX chain — gamma structure drifts
    # slowly and this keeps the extra API load friendly.
    intermarket_chain_secs: float = 60.0
    chain_strike_count: int = 40        # strikes each side for the SPY/QQQ chains
    # Intraday %-vs-prior-close deadband: a leg inside this is directionally NEUTRAL.
    pct_deadband: float = 0.05
    # Mechanical confidence boost (points) layered on Claude's verdict, clamped 95.
    align_boost_max: float = 10.0       # QQQ + SPY + regime all aligned
    partial_frac: float = 0.4           # partial alignment -> align_boost_max * this
    divergence_adj: float = 0.0         # QQQ opposes signal (≤0; 0 = no penalty)
    # |(bidSize - askSize)/(bidSize + askSize)| worth surfacing as an ES tilt.
    es_imbalance_min: float = 0.20
    # OPPOSING /ES order-book imbalance — graded as a real but PARTIAL failure tell
    # (it flagged several large-MAE losers, but missed the two worst blowups and
    # one fresh-low winner won with +0.63 against it). So this is priced as a
    # MODERATE post-verdict DEMOTION, never a veto: a clean fresh-extreme call keeps
    # most of its conviction, while a marginal mid-range one drops below the alert
    # band. A flat penalty keyed on |imbalance| > es_oppose_min opposing the trade.
    es_oppose_min: float = 0.40
    es_oppose_penalty: float = 6.0      # points subtracted (>=0; applied as -value)


@dataclass(frozen=True)
class StreamConfig:
    """Real-time schwab-py StreamClient feed — an additive, flag-gated websocket
    layer the engines prefer when live and fall back from to the REST poll path
    when disabled or stale. Wires the streamable, high-value services:
    level_one_option (ATM call/put → true premium OHLC with wicks → MOC),
    level_one_futures (/ES microstructure → intermarket), and chart_equity (SPY
    authoritative 1-min OHLCV → tape volume). $SPX index isn't streamable (it's a
    screener-only symbol), so the index price tape stays poll-based.
    See helenus/data/schwab_stream.py."""
    enabled: bool = True
    subscribe_options: bool = True
    subscribe_es: bool = True
    subscribe_spy: bool = True
    # A streamed datum older than this is "not fresh" → the consumer falls back to
    # polling. Level-one option/futures tick continuously (tight gate); the SPY
    # chart only prints once a minute, so it gets a looser one.
    stale_after_secs: float = 30.0
    chart_stale_secs: float = 90.0
    # Reconnect backoff after a dropped socket (exponential, capped).
    reconnect_backoff_secs: float = 5.0
    max_backoff_secs: float = 60.0


@dataclass(frozen=True)
class CumDeltaConfig:
    """Cumulative-delta exhaustion on /ES — a LEADING reversal read (a different
    CLASS of signal from the lagging confirmation triggers, with genuine causal
    lead time). Cumulative delta is the running sum of executed/aggressor /ES
    volume; when price makes a new session extreme but delta does not confirm it,
    the move is being absorbed and is spent BEFORE price reverses. Two outputs: a
    VETO/conviction-cap on reactive triggers firing INTO absorption, and an early
    reversal ARM when the divergence appears at a structural level.

    Built entirely from the level_one_futures /ES stream Helenus already
    subscribes to — no new feed. Stream-only by construction (the 30s REST poll is
    too coarse for executed-flow classification): when the /ES stream is stale or
    disabled there is no CD signal and every consumer no-ops. See
    helenus/engine/cumdelta.py."""
    # Master flag. Defaults OFF so current behavior is exactly preserved until
    # flipped on — every new call site is a no-op while the bot feeds no reading.
    enabled: bool = False
    # Contracts of opposing cumulative delta (vs the delta water-mark recorded at
    # the shelf) needed before a price extreme counts as absorbed.
    min_divergence: float = 1500.0
    # How close to the running extreme a tick counts as "at the shelf" — the
    # repeated 7340-7350 re-tags must register against the same shelf (index pts).
    retag_tolerance_pts: float = 2.0
    # VETO behavior on a reactive trigger firing INTO opposing absorption:
    #   "cap"  — clamp Claude's confidence to veto_conf_cap (keeps the alert, de-risked)
    #   "hard" — suppress the alert entirely (return no signal).
    veto_mode: str = "cap"
    veto_conf_cap: float = 40.0          # confidence ceiling under "cap" (below the
                                         # 53-66 band that didn't discriminate outcomes)
    # Reversal ARM only fires within this many points of the matching session
    # extreme (the "divergence at a structural level" gate; reuses edge-proximity
    # semantics so interior pins don't arm).
    arm_edge_pts: float = 10.0


@dataclass(frozen=True)
class ESLeadConfig:
    """ES-leads-SPX micro-lead. The $SPX cash index isn't streamable, so the price
    tape is folded from ~15s polls and each bar close is up to ~15-30s stale. /ES is
    streamed tick-by-tick and futures lead the cash index, so a genuine /ES thrust
    ARMS a direction; scan2 fires an ES-led momentum candidate the moment the SPX
    bar starts confirming it — up to one bar earlier than the SPX-only range
    expansion. Re-timing, not de-tuning: the substance is the (mature) /ES break
    plus cash confirmation. Only /ES displacement is used (basis-irrelevant).

    Default OFF so current behavior is exactly preserved; behind the stream
    staleness gate, the poll-only path is unchanged when /ES is stale/disabled.
    See helenus/engine/es_lead.py and scan2.detect_candidate (ES_LEAD)."""
    enabled: bool = False
    lookback_bars: int = 3              # /ES thrust window (shorter than SPX's 5 — we want the lead)
    atr_mult: float = 2.0              # |/ES displacement| must clear this × ES-ATR
    min_pts: float = 4.0               # ...AND this absolute /ES points floor (dual gate)
    arm_ttl_bars: int = 2              # bars the /ES arm waits for SPX to confirm
    confirm_min_pts: float = 1.0       # min SPX move (this bar, arm direction) that confirms
    es_history: int = 30               # /ES bar-close samples kept


@dataclass(frozen=True)
class ApproachConfig:
    """Approach / arm — stage 1 of the anticipatory two-stage signal. Fires a
    low-confidence WATCH when price is APPROACHING a high-probability reaction zone
    (GEX wall, zero-gamma flip, charm wall, session extreme) with aligned
    structural context, BEFORE the reactive trigger could fire. The existing
    reactive trigger stays the entry, but fires pre-contextualized with confidence
    pre-loaded from the arm. Re-uses data Helenus already computes (the key-level
    grid, charm/regime/flow context, the Task-1 cumulative-delta read).

    Default OFF so current behavior is exactly preserved until flipped on — the arm
    detector is then un-consulted and no WATCH is posted. See
    helenus/engine/scan2.py:detect_approach and helenus/output/embeds.py:watch_embed."""
    enabled: bool = False
    # "Approaching" tolerance in index points — wider than level_proximity_pts (3)
    # because it's anticipatory, but tighter than a full range.
    approach_pts: float = 7.0
    # How long an arm stays live waiting for the reactive trigger (bars).
    arm_ttl_bars: int = 5
    # Bounded points added to an aligned entry's confidence (clamped 95 at the call
    # site). Scales with how many context legs align (1 leg → half, 2+ → full).
    confidence_preload: float = 8.0
    # Proximity alone never arms — a reaction context (charm/regime/flow/CD) must back it.
    require_context: bool = True


@dataclass(frozen=True)
class GexMapConfig:
    """GEX-as-primary-frame — the persistent, spatial map every other engine is
    scored against. GEX is a MAP, not a trigger: it says WHERE reactions are likely
    and WHETHER a setup agrees with structure; the interaction signals (cross,
    displacement, flow inflection, CD divergence, approach-arm) still own the entry
    TIMING. A bare GEX-level touch never fires an alert.

    `gex.py` stays pure/stateless; the maintained model is a thin stateful wrapper
    (helenus/engine/gexmap.py:GexMapTracker, mirroring flow.VannaTracker) updated
    each chain poll. It holds every wall with a PERSISTENCE count (a wall that has
    held across polls is stronger; a fresh one is provisional), the zero-gamma flip,
    the regime, and derives price's POSITION within the gamma envelope (nearest wall
    above/below, the cell price occupies, and a position_state). From the map alone
    it emits a SYMMETRIC directional prior (positive gamma fades BOTH walls equally),
    which gates/scores every other engine's candidate — agreeing + room + favorable
    regime promotes it; into a defended wall / against the prior / no room turns it
    OFF (not merely lowered).

    Default OFF so current behavior is exactly preserved until flipped on — the state
    model, the snapshot blocks, and the gating are all dormant while disabled (the
    Measurement task validates on the journal first). See gexmap.py, scan2 (the
    pre-Claude gate), and analyst.py (the leading snapshot blocks + post-verdict OFF)."""
    enabled: bool = False
    # Spot within this of a wall / zero-Γ reads as PINNED_AT_WALL (reuses the
    # scan "at a level" tolerance).
    pin_proximity_pts: float = 3.0
    # Room-to-opposing-magnet floors, mirroring ScalpConfig.min_room_pts / the
    # analyst's graduated room rule: momentum edges need only ~4pt, everything else
    # ~8pt, and a stacked same-side wall cluster keeps the full ~8pt regardless
    # (the graded c0fd93fa 8.6pt-into-three-put-walls failure).
    momentum_room_pts: float = 4.0
    default_room_pts: float = 8.0
    stacked_wall_room_pts: float = 8.0
    # Two walls within this of each other count as a STACKED same-side cluster
    # (heavy gamma that absorbs a move into it).
    stacked_wall_span_pts: float = 10.0
    # Polls a wall must persist (reappear in the chain) before it counts as STRONG
    # rather than provisional — feeds the re-test-fatigue / defended-wall reads.
    persistence_strong: int = 3
    # "Approaching" tolerance for APPROACHING_WALL / APPROACHING_FLIP and the
    # map-driven arm (reuses ApproachConfig.approach_pts semantics).
    approach_pts: float = 7.0


@dataclass(frozen=True)
class FeedbackConfig:
    """Accuracy feedback loop — MFE/MAE grading + the journal."""
    journal_path: str = "journal/helenus.jsonl"
    # Distilled, human-editable lessons the analyst loads back into its prompt.
    lessons_path: str = "journal/lessons.md"
    # Forward window an alert is graded over, in bars (60s each -> 30 ≈ 30 min).
    forward_window_bars: int = 30
    mfe_target_pts: float = 5.0         # favorable excursion to count as a hit
    mae_stop_pts: float = 5.0           # adverse excursion that marks a miss
    ratio_target: float = 1.5           # MFE/MAE needed to grade ACCURATE
    mae_floor_pts: float = 0.25         # ratio denominator floor (no div-by-zero)
    net_floor_pts: float = 0.0          # min held net_pts to grade ACCURATE (else MIXED)
    # Immediate-reversal metric: an alert whose adverse excursion in the FIRST
    # `early_window_bars` exceeds `early_reversal_pts` "fired then reversed" — the
    # earliness failure we want to drive DOWN (a reported metric, not a re-grade).
    early_window_bars: int = 4
    early_reversal_pts: float = 3.0
    reflect_each_alert: bool = False    # per-alert Claude note (adds one call each)
    review_hour: int = 16               # daily auto-review at the market close (ET)
    review_minute: int = 0
    review_max_alerts: int = 60         # cap alerts fed to a Claude review


@dataclass(frozen=True)
class HelenusConfig:
    gex: GexConfig = field(default_factory=GexConfig)
    charm: CharmConfig = field(default_factory=CharmConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    scalp: ScalpConfig = field(default_factory=ScalpConfig)
    displacement: DisplacementConfig = field(default_factory=DisplacementConfig)
    orb: ORBConfig = field(default_factory=ORBConfig)
    moc: MocConfig = field(default_factory=MocConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    flow: FlowConfig = field(default_factory=FlowConfig)
    analyst: AnalystConfig = field(default_factory=AnalystConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    intermarket: IntermarketConfig = field(default_factory=IntermarketConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    cumdelta: CumDeltaConfig = field(default_factory=CumDeltaConfig)
    approach: ApproachConfig = field(default_factory=ApproachConfig)
    es_lead: ESLeadConfig = field(default_factory=ESLeadConfig)
    gexmap: GexMapConfig = field(default_factory=GexMapConfig)


CONFIG = HelenusConfig()
