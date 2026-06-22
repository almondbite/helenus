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

MACRO_SYMBOLS: tuple[str, ...] = ("/CL", "/GC", "$VIX")

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
    bar_seconds: int = 60               # sequential interval size
    gex_cluster_proximity_pts: float = 10.0  # confluence credit distance


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
    """Options-volume tracking and the vanna-rally detector."""
    # VIX trend: compare the latest sample to one ~this many macro-samples ago.
    # macro_worker runs every 30s, so 6 samples ≈ 3 minutes.
    vix_lookback_samples: int = 6
    vix_drop_pts: float = 0.30          # VIX must fall at least this over lookback
    # Fresh OTM-call contracts (interval delta) needed before flow "counts".
    min_call_flow: float = 2000.0
    # OTM call flow must outpace OTM put flow by this multiple to be vanna-active.
    call_dominance_ratio: float = 1.5
    ladder_strikes: int = 8             # strikes each side of spot in the visual


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
    reflect_each_alert: bool = False    # per-alert Claude note (adds one call each)
    review_hour: int = 16               # daily auto-review time (ET)
    review_minute: int = 30
    review_max_alerts: int = 60         # cap alerts fed to a Claude review


@dataclass(frozen=True)
class HelenusConfig:
    gex: GexConfig = field(default_factory=GexConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)
    flow: FlowConfig = field(default_factory=FlowConfig)
    analyst: AnalystConfig = field(default_factory=AnalystConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)


CONFIG = HelenusConfig()
