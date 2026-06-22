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
from helenus.engine import flow as flow_engine
from helenus.engine import gex as gex_engine
from helenus.engine.analyst import ClaudeAnalyst
from helenus.engine.scan2 import (
    Bar,
    MarketState,
    Signal,
    TriggerType,
    detect_candidate,
)
from helenus.journal import (
    Journal,
    LessonStore,
    OpenAlert,
    OutcomeTracker,
    new_alert_id,
)
from helenus.output import embeds

log = logging.getLogger("helenus.bot")

ALERT_COOLDOWN_SECS = 300  # same trigger+level can't re-fire inside 5 min
# Same trigger+level can't spend a Claude call inside this window. Longer than
# the alert cooldown: it throttles the *gate*, so a level price pins (a wall it
# hovers on all session) costs one judgment, not a dozen. Tune with
# scripts/replay_gate.py against more sessions.
CANDIDATE_COOLDOWN_SECS = 600


class HelenusBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.feed = SchwabFeed()
        self.analyst = ClaudeAnalyst()
        self.state = MarketState()
        self.profile: gex_engine.GexProfile | None = None

        # Options-flow / vanna tracking
        self.vol_profile: flow_engine.VolumeProfile | None = None
        self.vanna = flow_engine.VannaTracker()
        self.vanna_reading: flow_engine.VannaReading | None = None

        # Accuracy feedback loop
        self.journal = Journal()
        self.tracker = OutcomeTracker()
        self.lessons = LessonStore()
        # Carry any prior learned lessons into this session's analysis.
        self.analyst.set_lessons(self.lessons.load())

        # Macro tape
        self.vix_history: deque[float] = deque(maxlen=500)
        self.macro_quotes: dict = {}
        self._last_spy_cum_volume: float | None = None
        self._interval_volume: float = 0.0
        # Intra-bar extremes, folded from chain-poll spot samples between bar
        # closes. Without a streaming feed this is how Bar.high/low get real
        # range instead of collapsing to the close — see _observe_spot.
        self._bar_high: float = float("-inf")
        self._bar_low: float = float("inf")

        self._alert_log: dict[str, float] = {}
        self._candidate_log: dict[str, float] = {}
        self._premarket_sent_on: dt.date | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def setup_hook(self) -> None:
        await self.feed.connect()
        self.add_command(cmd_gex)
        self.add_command(cmd_scan)
        self.add_command(cmd_flow)
        self.add_command(cmd_stats)
        self.add_command(cmd_review)
        # chain_worker self-paces, so it's a raw task; the rest are tasks.loop
        self._chain_task = asyncio.create_task(self._chain_worker(), name="chain_worker")
        self.bar_worker.start()
        self.macro_worker.start()
        self.premarket_worker.start()
        self.review_worker.start()

    async def on_ready(self) -> None:
        log.info("Helenus online as %s", self.user)

    async def close(self) -> None:
        # _chain_task only exists once setup_hook ran far enough; guard so a
        # startup failure (e.g. feed.connect) surfaces its real error instead
        # of an AttributeError raised here during teardown.
        task = getattr(self, "_chain_task", None)
        if task is not None:
            task.cancel()
        await self.feed.close()
        await self.analyst.aclose()
        await super().close()

    def alert_channel(self) -> discord.abc.Messageable | None:
        return self.get_channel(DISCORD_CHANNEL_ID)

    # ------------------------------------------------------------------ #
    # Workers
    # ------------------------------------------------------------------ #

    async def _chain_worker(self) -> None:
        """Adaptive-cadence 0DTE chain poll -> GexProfile."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if market_session() != "closed":
                    payload = await self.feed.fetch_0dte_chain()
                    self.profile = gex_engine.build_profile(payload)
                    # Each chain poll is a fresh spot sample (~15s); fold it into
                    # the forming bar so high/low capture true intra-bar range.
                    self._observe_spot(self.profile.spot)
                    self.vol_profile = flow_engine.build_volume_profile(payload)
                    self.vanna_reading = self.vanna.update(
                        self.vol_profile, list(self.vix_history)
                    )
                    log.debug(
                        "GEX refreshed: spot=%.2f zeroΓ=%s vanna=%s",
                        self.profile.spot,
                        self.profile.zero_gamma,
                        self.vanna_reading.label if self.vanna_reading else "n/a",
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
        if spot <= 0:
            return  # broken chain payload — don't poison the tape / extremes
        self._observe_spot(spot)  # ensure the closing print is in the range
        bar = Bar(
            ts=now_et(),
            open=self.state.bars[-1].close if self.state.bars else spot,
            high=self._bar_high,
            low=self._bar_low,
            close=spot,
            volume=self._interval_volume,
        )
        self._interval_volume = 0.0
        # Reset extremes for the next interval (after the close was folded in).
        self._bar_high = float("-inf")
        self._bar_low = float("inf")
        self.state.push_bar(bar)
        self.feed.throttle.report_realized_range(self.state.realized_range_5m())

        # Roll MFE/MAE for every open alert and finalize any that matured.
        for open_alert, outcome in self.tracker.update(spot):
            await self._finalize_outcome(open_alert, outcome)

        # Cheap gate decides whether this bar is worth a Claude call; Claude
        # makes the actual judgment. An active vanna setup, a sweep, or a
        # level-cross-on-volume can trip it. Most bars don't trip it at all.
        candidate = detect_candidate(self.state, self.profile, self.vanna_reading)
        if candidate is None or self._candidate_throttled(candidate):
            return
        sig = await self.analyst.analyze(
            self.state,
            self.profile,
            self.vol_profile,
            self.vanna_reading,
            self.macro_quotes,
            candidate,
        )
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

    @tasks.loop(
        time=dt.time(
            hour=CONFIG.feedback.review_hour,
            minute=CONFIG.feedback.review_minute,
            tzinfo=ET,
        )
    )
    async def review_worker(self) -> None:
        """Daily accuracy review — Claude reads the graded journal for patterns."""
        review = await self._run_review()
        if review is None:
            return
        channel = self.alert_channel()
        if channel:
            await channel.send(
                embed=embeds.review_embed(review, self.journal.stats())
            )

    @review_worker.before_loop
    async def _review_ready(self) -> None:
        await self.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _observe_spot(self, spot: float) -> None:
        """Fold a fresh spot sample into the forming bar's high/low extremes."""
        if spot <= 0:
            return
        self._bar_high = max(self._bar_high, spot)
        self._bar_low = min(self._bar_low, spot)

    def _candidate_throttled(self, candidate) -> bool:
        """True if this trigger+level already spent a Claude call recently.

        Keyed on trigger+level so a level price pins (e.g. a GEX wall it hovers
        on through a range session) is judged once per window, not every bar it
        keeps tagging. Vanna candidates carry no level — throttled per trigger.
        """
        lvl = f"{candidate.level.price:.0f}" if candidate.level else "na"
        key = f"{candidate.trigger.value}:{lvl}"
        now = asyncio.get_running_loop().time()
        if now - self._candidate_log.get(key, -1e9) < CANDIDATE_COOLDOWN_SECS:
            return True
        self._candidate_log[key] = now
        return False

    def _record_alert(self, sig: Signal) -> None:
        """Open MFE/MAE tracking for an intraday alert and journal it."""
        if sig.trigger is TriggerType.PREMARKET_SETUP:
            return  # a briefing, not an intraday entry — not price-graded
        p, vp, vn = self.profile, self.vol_profile, self.vanna_reading
        context = {
            "trigger": sig.trigger.value,
            "direction": sig.direction.value,
            "confidence": sig.confidence,
            "trend_label": sig.trend_label,
            "level": f"{sig.level.label} {sig.level.price:.2f}" if sig.level else None,
            "thesis": sig.notes[0] if sig.notes else None,
            "regime": p.regime if p else None,
            "zero_gamma": round(p.zero_gamma, 1) if p and p.zero_gamma is not None else None,
            "nearest_wall_dist": round(p.nearest_cluster_distance(sig.spot), 2) if p else None,
            "vanna_active": vn.active if vn else None,
            "vanna_label": vn.label if vn else None,
            "otm_call_put_ratio": round(vp.otm_call_put_ratio, 2) if vp else None,
            "vol_above_spot": round(vp.above_spot_vol) if vp else None,
            "vol_below_spot": round(vp.below_spot_vol) if vp else None,
        }
        alert = OpenAlert(
            id=new_alert_id(),
            ts_open=now_et().isoformat(),
            trigger=sig.trigger.value,
            direction=sig.direction.value,
            entry=sig.spot,
            confidence=sig.confidence,
            context=context,
        )
        self.tracker.track(alert)
        self.journal.log_alert(alert)

    async def _finalize_outcome(self, alert: OpenAlert, outcome) -> None:
        """Log a matured alert's MFE/MAE grade (+ optional Claude reflection)."""
        note = None
        if CONFIG.feedback.reflect_each_alert:
            note = await self.analyst.reflect(
                {
                    "trigger": alert.trigger,
                    "direction": alert.direction,
                    "entry": round(alert.entry, 2),
                    "confidence": alert.confidence,
                    "context": alert.context,
                },
                {
                    "grade": outcome.grade,
                    "mfe_pts": outcome.mfe_pts,
                    "mae_pts": outcome.mae_pts,
                    "mfe_mae_ratio": outcome.mfe_mae_ratio,
                    "net_pts": outcome.net_pts,
                    "bars": outcome.bars,
                },
            )
        self.journal.log_outcome(alert.id, outcome, note)
        log.info(
            "Graded %s %s: MFE %.1f MAE %.1f ratio %.2f",
            alert.trigger, outcome.grade, outcome.mfe_pts,
            outcome.mae_pts, outcome.mfe_mae_ratio,
        )

    async def _run_review(self) -> dict | None:
        graded = self.journal.graded_alerts()
        if not graded:
            return None
        digest = graded[-CONFIG.feedback.review_max_alerts:]
        review = await self.analyst.review_patterns(digest)
        if review is not None:
            self.journal.log_review(review)
            # Close the loop: persist distilled lessons and feed them back into
            # the analyst's prompt for every subsequent judgment.
            text = self.lessons.save(review, self.journal.stats())
            self.analyst.set_lessons(text)
        return review

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
        return await self.analyst.premarket_briefing(es, cl, vix_last, vix_band)

    async def _dispatch(self, sig: Signal) -> None:
        """Cooldown-gated alert post."""
        key = f"{sig.trigger.value}:{sig.level.price if sig.level else 'na'}:{sig.direction.value}"
        now = asyncio.get_running_loop().time()
        if now - self._alert_log.get(key, -1e9) < ALERT_COOLDOWN_SECS:
            return
        self._alert_log[key] = now
        # Open MFE/MAE tracking before posting — grade it regardless of Discord.
        self._record_alert(sig)
        channel = self.alert_channel()
        if not channel:
            return
        await channel.send(
            embed=embeds.signal_embed(
                sig, self.profile, self.vol_profile, self.vanna_reading
            )
        )
        # A vanna-driven call is about the volume distribution — show the ladder.
        if sig.trigger is TriggerType.VANNA_RALLY and self.vol_profile is not None:
            await channel.send(
                embed=embeds.volume_profile_embed(self.vol_profile, self.vanna_reading)
            )


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
    levels = st.key_levels(spot, bot.profile) if spot == spot else []
    lines = [
        f"Spot: `{spot:.2f}`" if spot == spot else "Spot: `warming up`",
        f"Bars: `{len(st.bars)}` | Vol ratio: `{ratio:.2f}x`" if ratio == ratio
        else f"Bars: `{len(st.bars)}` | Vol ratio: `building baseline`",
        f"Trend: `{trend.value if trend else 'NONE'}`",
        "Levels: " + ", ".join(f"`{lv.label} {lv.price:.0f}`" for lv in levels[:6]),
    ]
    await ctx.send("\n".join(lines))


@commands.command(name="flow")
async def cmd_flow(ctx: commands.Context) -> None:
    """!flow — post the 0DTE options-volume ladder + vanna read."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.vol_profile is None:
        await ctx.send("No 0DTE volume snapshot yet — engine warming up.")
        return
    await ctx.send(
        embed=embeds.volume_profile_embed(bot.vol_profile, bot.vanna_reading)
    )


@commands.command(name="stats")
async def cmd_stats(ctx: commands.Context) -> None:
    """!stats — deterministic MFE/MAE accuracy scorecard (no Claude call)."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    s = bot.journal.stats()
    if s.get("count", 0) == 0:
        await ctx.send(f"No graded alerts yet ({bot.tracker.open_count} still maturing).")
        return
    lines = [
        f"Graded: `{s['count']}` | Accuracy: **{s['accuracy_pct']}%** "
        f"(`{bot.tracker.open_count}` maturing)",
        f"✓ `{s['accurate']}`  ~ `{s['mixed']}`  ✗ `{s['inaccurate']}`",
        f"Avg MFE `{s['avg_mfe_pts']}` / MAE `{s['avg_mae_pts']}` pts | ratio `{s['avg_ratio']}`",
    ]
    for trig, d in s.get("by_trigger", {}).items():
        lines.append(f"`{trig}`: {d['ACCURATE']}/{d['n']} accurate")
    await ctx.send("\n".join(lines))


@commands.command(name="review")
async def cmd_review(ctx: commands.Context) -> None:
    """!review — run a Claude accuracy review over the graded journal now."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    await ctx.send("Running accuracy review…")
    review = await bot._run_review()
    if review is None:
        await ctx.send("No graded alerts to review yet.")
        return
    await ctx.send(embed=embeds.review_embed(review, bot.journal.stats()))
