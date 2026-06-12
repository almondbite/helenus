"""
Helenus event loop — discord.py owns the asyncio loop; everything else rides it.

Loop topology:
    chain_worker   : adaptive cadence — $SPXW chain -> GexProfile (throttled)
    bar_worker     : fixed cadence  — assembles Bars, runs Triggers 1 & 2
    macro_worker   : fixed cadence  — /CL, /GC, $VIX, SPY volume proxy
    premarket_worker: fires once per session before the open (Trigger 3)

All math is synchronous Pandas inside the workers; only network I/O awaits.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import deque

import discord
from discord.ext import commands, tasks

from helenus.config import CONFIG, DISCORD_CHANNEL_ID
from helenus.data.schwab_feed import ET, SchwabFeed, market_session, now_et
from helenus.engine import gex as gex_engine
from helenus.engine.scan2 import (
    Bar,
    MarketState,
    Signal,
    check_premarket_setup,
    check_sweep_recover,
    check_volume_confirmation,
)
from helenus.output import embeds

log = logging.getLogger("helenus.bot")

ALERT_COOLDOWN_SECS = 300  # same trigger+level can't re-fire inside 5 min


class HelenusBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.feed = SchwabFeed()
        self.state = MarketState()
        self.profile: gex_engine.GexProfile | None = None

        # Macro tape
        self.vix_history: deque[float] = deque(maxlen=500)
        self.macro_quotes: dict = {}
        self._last_spy_cum_volume: float | None = None
        self._interval_volume: float = 0.0

        self._alert_log: dict[str, float] = {}
        self._premarket_sent_on: dt.date | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def setup_hook(self) -> None:
        await self.feed.connect()
        self.add_command(cmd_gex)
        self.add_command(cmd_scan)
        # chain_worker self-paces, so it's a raw task; the rest are tasks.loop
        self._chain_task = asyncio.create_task(self._chain_worker(), name="chain_worker")
        self.bar_worker.start()
        self.macro_worker.start()
        self.premarket_worker.start()

    async def on_ready(self) -> None:
        log.info("Helenus online as %s", self.user)

    async def close(self) -> None:
        self._chain_task.cancel()
        await self.feed.close()
        await super().close()

    def alert_channel(self) -> discord.abc.Messageable | None:
        return self.get_channel(DISCORD_CHANNEL_ID)

    # ------------------------------------------------------------------ #
    # Workers
    # ------------------------------------------------------------------ #

    async def _chain_worker(self) -> None:
        """Adaptive-cadence $SPXW chain poll -> GexProfile."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if market_session() != "closed":
                    payload = await self.feed.fetch_0dte_chain()
                    self.profile = gex_engine.build_profile(payload)
                    log.debug(
                        "GEX refreshed: spot=%.2f zeroΓ=%s",
                        self.profile.spot,
                        self.profile.zero_gamma,
                    )
            except Exception:
                log.exception("chain_worker cycle failed")
            await asyncio.sleep(self.feed.throttle.chain_interval())

    @tasks.loop(seconds=CONFIG.scan.bar_seconds)
    async def bar_worker(self) -> None:
        """Closes a bar each interval and evaluates Triggers 1 & 2."""
        if market_session() != "regular" or self.profile is None:
            return
        spot = self.profile.spot
        bar = Bar(
            ts=now_et(),
            open=self.state.bars[-1].close if self.state.bars else spot,
            high=spot,   # structural placeholder — see README: wire intra-bar
            low=spot,    # high/low from a streaming quote for production
            close=spot,
            volume=self._interval_volume,
        )
        self._interval_volume = 0.0
        self.state.push_bar(bar)
        self.feed.throttle.report_realized_range(self.state.realized_range_5m())

        for check in (check_volume_confirmation, check_sweep_recover):
            sig = check(self.state, self.profile)
            if sig is not None:
                await self._dispatch(sig)

    @bar_worker.before_loop
    async def _bar_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=CONFIG.throttle.macro_secs)
    async def macro_worker(self) -> None:
        """Macro board refresh: /CL, /GC, $VIX bands, SPY volume proxy."""
        try:
            quotes = await self.feed.fetch_macro_quotes()
        except Exception:
            log.exception("macro_worker fetch failed")
            return
        self.macro_quotes = quotes

        vix = (quotes.get("$VIX", {}).get("quote") or {}).get("lastPrice")
        if vix:
            self.vix_history.append(float(vix))

        spy_vol = (quotes.get("SPY", {}).get("quote") or {}).get("totalVolume")
        if spy_vol is not None:
            if self._last_spy_cum_volume is not None:
                self._interval_volume += max(0.0, spy_vol - self._last_spy_cum_volume)
            self._last_spy_cum_volume = float(spy_vol)

    @macro_worker.before_loop
    async def _macro_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(time=dt.time(hour=9, minute=0, tzinfo=ET))
    async def premarket_worker(self) -> None:
        """Trigger 3 — one briefing per session at 09:00 ET."""
        today = now_et().date()
        if self._premarket_sent_on == today or market_session() == "closed":
            return
        try:
            sig, macro_lines = await self._build_premarket()
        except Exception:
            log.exception("premarket routine failed")
            return
        channel = self.alert_channel()
        if channel:
            await channel.send(embed=embeds.premarket_embed(sig, macro_lines))
            self._premarket_sent_on = today

    @premarket_worker.before_loop
    async def _premarket_ready(self) -> None:
        await self.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _build_premarket(self) -> tuple[Signal, list[str]]:
        await self.feed.throttle.gate()
        resp = await self.feed._client.get_quotes(["/ES", "/CL", "$VIX"])
        resp.raise_for_status()
        q = resp.json()

        def quote(sym: str) -> dict:
            return q.get(sym, {}).get("quote") or {}

        es = quote("/ES")
        cl = quote("/CL")
        vix_last = float(quote("$VIX").get("lastPrice") or 0.0)
        vix_band = (
            (min(self.vix_history), max(self.vix_history))
            if len(self.vix_history) >= 10
            else (vix_last - 1.5, vix_last + 1.5)  # cold-start fallback band
        )
        sig = check_premarket_setup(
            es_last=float(es.get("lastPrice") or 0.0),
            es_prior_close=float(es.get("closePrice") or 0.0),
            vix_last=vix_last,
            vix_band=vix_band,
            cl_change_pct=float(cl.get("netPercentChange") or 0.0),
        )
        macro_lines = [
            f"/ES `{es.get('lastPrice')}` ({es.get('netPercentChange', 0):+.2f}%)",
            f"/CL `{cl.get('lastPrice')}` ({cl.get('netPercentChange', 0):+.2f}%)",
            f"$VIX `{vix_last:.2f}` band {vix_band[0]:.2f}–{vix_band[1]:.2f}",
        ]
        return sig, macro_lines

    async def _dispatch(self, sig: Signal) -> None:
        """Cooldown-gated alert post."""
        key = f"{sig.trigger.value}:{sig.level.price if sig.level else 'na'}:{sig.direction.value}"
        now = asyncio.get_running_loop().time()
        if now - self._alert_log.get(key, -1e9) < ALERT_COOLDOWN_SECS:
            return
        self._alert_log[key] = now
        channel = self.alert_channel()
        if channel:
            await channel.send(embed=embeds.signal_embed(sig, self.profile))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

@commands.command(name="gex")
async def cmd_gex(ctx: commands.Context) -> None:
    """!gex — post the current $SPXW gamma structure board."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.profile is None:
        await ctx.send("No chain snapshot yet — engine warming up.")
        return
    await ctx.send(embed=embeds.gex_snapshot_embed(bot.profile))


@commands.command(name="scan")
async def cmd_scan(ctx: commands.Context) -> None:
    """!scan — show scan2 internals: volume baseline, levels, trend."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    st = bot.state
    spot = bot.profile.spot if bot.profile else float("nan")
    ratio = st.volume_ratio()
    trend = st.trend_direction()
    levels = st.key_levels(spot) if spot == spot else []
    lines = [
        f"Spot: `{spot:.2f}`" if spot == spot else "Spot: `warming up`",
        f"Bars: `{len(st.bars)}` | Vol ratio: `{ratio:.2f}x`" if ratio == ratio
        else f"Bars: `{len(st.bars)}` | Vol ratio: `building baseline`",
        f"Trend: `{trend.value if trend else 'NONE'}`",
        "Levels: " + ", ".join(f"`{lv.label} {lv.price:.0f}`" for lv in levels[:6]),
    ]
    await ctx.send("\n".join(lines))
