"""
Async ingestion layer over the Charles Schwab Developer API (schwab-py).

Responsibilities:
  * Authenticate and hold a single schwab-py AsyncClient.
  * Pull the 0DTE chain via the $SPX underlying, keeping only the PM-settled
    SPXW contracts (NOT the AM-settled $SPX monthlies).
  * Pull macro quotes: /CL, /GC, $VIX, plus the SPY volume proxy.
  * Enforce adaptive throttling so we stay friendly with rate limits when
    nothing is moving.

This module returns *raw payloads only*. All math happens in helenus.engine.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from zoneinfo import ZoneInfo

from schwab.auth import client_from_token_file

from helenus.config import (
    CHAIN_CONTRACT_ROOT,
    CHAIN_SYMBOL,
    CONFIG,
    MACRO_SYMBOLS,
    PROXY_SYMBOL,
    SCHWAB_API_KEY,
    SCHWAB_APP_SECRET,
    SCHWAB_TOKEN_PATH,
)

log = logging.getLogger("helenus.feed")


def _filter_contract_root(payload: dict[str, Any], root: str) -> None:
    """
    Drop every contract whose symbol root isn't `root`, in place.

    Schwab nests contracts as {expDateStr: {strikeStr: [contract, ...]}}. On the
    monthly expiry the $SPX chain returns both PM-settled SPXW dailies and the
    AM-settled SPX monthly under the same date key; keeping only the SPXW root
    stops the two settlement regimes from being aggregated together. Empty
    strike and expiration buckets are pruned so downstream flattening is clean.
    """
    for map_key in ("callExpDateMap", "putExpDateMap"):
        exp_map = payload.get(map_key)
        if not exp_map:
            continue
        for exp_key in list(exp_map):
            strikes = exp_map[exp_key]
            for strike_key in list(strikes):
                kept = [
                    c for c in strikes[strike_key]
                    if str(c.get("symbol", "")).startswith(root)
                ]
                if kept:
                    strikes[strike_key] = kept
                else:
                    del strikes[strike_key]
            if not strikes:
                del exp_map[exp_key]

ET = ZoneInfo("America/New_York")


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=ET)


def market_session(ts: dt.datetime | None = None) -> str:
    """Coarse session bucket: 'premarket' | 'regular' | 'closed'."""
    ts = ts or now_et()
    if ts.weekday() >= 5:
        return "closed"
    t = ts.time()
    if dt.time(4, 0) <= t < dt.time(9, 30):
        return "premarket"
    if dt.time(9, 30) <= t < dt.time(16, 15):  # SPXW 0DTE settles 16:15 ET
        return "regular"
    return "closed"


class AdaptiveThrottle:
    """
    Gatekeeper between the loops and the API.

    Two jobs:
      1. A hard minimum gap between *any* two Schwab calls (burst protection).
      2. A dynamic chain-poll interval: tight in regular hours, relaxed in
         premarket, and backed off further when realized volatility is dead.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._low_vol = False

    def report_realized_range(self, range_5m_pts: float) -> None:
        """scan2 feeds the rolling 5-minute spot range back in here."""
        self._low_vol = range_5m_pts < CONFIG.throttle.low_vol_range_pts

    def chain_interval(self) -> float:
        session = market_session()
        if session == "premarket":
            return CONFIG.throttle.premarket_chain_secs
        if session == "closed":
            return CONFIG.throttle.slow_chain_secs * 5
        if self._low_vol:
            return CONFIG.throttle.slow_chain_secs
        return CONFIG.throttle.base_chain_secs

    async def gate(self) -> None:
        """Await until the hard minimum call gap has elapsed."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last_call + CONFIG.throttle.min_call_gap_secs - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()


class SchwabFeed:
    """Owns the AsyncClient and exposes typed fetchers."""

    def __init__(self) -> None:
        self._client: Any = None
        self.throttle = AdaptiveThrottle()

    async def connect(self) -> None:
        # asyncio=True hands back an AsyncClient (httpx under the hood).
        # Token must already exist on disk — run scripts/authorize.py once
        # interactively to mint it; the bot itself never opens a browser.
        self._client = client_from_token_file(
            token_path=SCHWAB_TOKEN_PATH,
            api_key=SCHWAB_API_KEY,
            app_secret=SCHWAB_APP_SECRET,
            asyncio=True,
        )
        log.info("Schwab AsyncClient ready")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close_async_session()

    # ------------------------------------------------------------------ #
    # Options chain
    # ------------------------------------------------------------------ #

    async def fetch_0dte_chain(self) -> dict[str, Any]:
        """
        Raw 0DTE chain JSON for today's expiration only.

        The chains endpoint only accepts the underlying ("$SPX"); "$SPXW" 400s.
        Pinning from_date == to_date == today isolates today's expiration, and
        _filter_contract_root keeps only the PM-settled SPXW contracts (the AM-
        settled monthly shares the date on the 3rd Friday).
        """
        await self.throttle.gate()
        today = now_et().date()
        resp = await self._client.get_option_chain(
            CHAIN_SYMBOL,
            contract_type=self._client.Options.ContractType.ALL,
            strike_count=CONFIG.gex.strike_count,
            from_date=today,
            to_date=today,
            include_underlying_quote=True,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") not in (None, "SUCCESS"):
            log.warning("Chain status=%s", payload.get("status"))
        # Strip AM-settled monthlies that share today's date on the 3rd Friday.
        _filter_contract_root(payload, CHAIN_CONTRACT_ROOT)
        return payload

    # ------------------------------------------------------------------ #
    # Macro & proxy quotes
    # ------------------------------------------------------------------ #

    async def fetch_macro_quotes(self) -> dict[str, Any]:
        """Single batched quote call: /CL, /GC, $VIX + SPY volume proxy."""
        await self.throttle.gate()
        symbols = list(MACRO_SYMBOLS) + [PROXY_SYMBOL]
        resp = await self._client.get_quotes(symbols)
        resp.raise_for_status()
        return resp.json()
