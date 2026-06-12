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

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------

# CRITICAL: $SPXW isolates the PM-settled weekly root used for 0DTE.
# $SPX would return the AM-settled monthlies and pollute intraday GEX.
CHAIN_SYMBOL: str = "$SPXW"
UNDERLYING_SYMBOL: str = "$SPX"
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
    round_number_step: float = 10.0     # SPX round-number grid (7430, 7440, ...)
    major_round_step: float = 50.0      # heavier weight grid (7400, 7450, ...)
    level_proximity_pts: float = 3.0    # "at a level" tolerance in index points
    sweep_pierce_pts: float = 2.0       # Trigger 2: minimum pierce depth
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
class HelenusConfig:
    gex: GexConfig = field(default_factory=GexConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    throttle: ThrottleConfig = field(default_factory=ThrottleConfig)


CONFIG = HelenusConfig()
