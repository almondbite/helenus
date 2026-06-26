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

from helenus.config import CONFIG, DISCORD_CHANNEL_ID, PROXY_SYMBOL, UNDERLYING_SYMBOL
from helenus.data.schwab_feed import ET, SchwabFeed, market_session, now_et
from helenus.data import schwab_stream
from helenus.engine import charm as charm_engine
from helenus.engine import flow as flow_engine
from helenus.engine import gex as gex_engine
from helenus.engine import gexmap as gexmap_engine
from helenus.engine import intermarket as intermarket_engine
from helenus.engine import scalp as scalp_engine
from helenus.engine import displacement as displacement_engine
from helenus.engine import orb as orb_engine
from helenus.engine import moc as moc_engine
from helenus.engine import cumdelta as cumdelta_engine
from helenus.engine import es_lead as es_lead_engine
from helenus.engine.analyst import ClaudeAnalyst
from helenus.engine.scan2 import (
    ApproachArm,
    Bar,
    Direction,
    MarketState,
    Signal,
    TriggerType,
    detect_approach,
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


def _candles_to_bars(price_candles: list[dict], volume_candles: list[dict]) -> list[Bar]:
    """Merge $SPX 1-min OHLC with SPY 1-min volume (same minute timestamps) into
    Bars for tape backfill. SPX prints no volume, so the SPY proxy supplies it —
    matching how the live tape is built. Pure; testable without a feed."""
    spy_vol = {c["datetime"]: c.get("volume", 0) for c in volume_candles}
    bars: list[Bar] = []
    for c in price_candles:
        bars.append(
            Bar(
                ts=dt.datetime.fromtimestamp(c["datetime"] / 1000, tz=ET),
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(spy_vol.get(c["datetime"], 0.0)),
            )
        )
    return bars


class HelenusBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.feed = SchwabFeed()
        # Real-time websocket feed (additive, flag-gated). When live the engines
        # prefer streamed data (true option-premium OHLC, /ES microstructure, SPY
        # 1-min volume); when off/stale they fall back to the REST poll path.
        self.stream = schwab_stream.SchwabStream(self.feed)
        self.analyst = ClaudeAnalyst()
        self.state = MarketState()
        self.profile: gex_engine.GexProfile | None = None
        # GEX-as-primary-frame — the persistent spatial map (walls + persistence,
        # zero-Γ, regime, price's position in the envelope) and its directional prior.
        # Maintained each chain poll; gates/scores every other candidate when enabled.
        # None while CONFIG.gexmap.enabled is off (current behavior preserved).
        self.gexmap_tracker = gexmap_engine.GexMapTracker()
        self.gex_map: gexmap_engine.GexMapState | None = None
        # Charm (delta-decay) structure — the afternoon-drift bias.
        self.charm_profile: charm_engine.CharmProfile | None = None

        # Options-flow / vanna tracking
        self.vol_profile: flow_engine.VolumeProfile | None = None
        self.vanna = flow_engine.VannaTracker()
        self.vanna_reading: flow_engine.VannaReading | None = None

        # EMA-ignition contract scalp — index 5/9/200 EMAs + the gated cross.
        # Stateful (mirrors VannaTracker): on_chain per poll, on_bar per bar.
        self.scalp = scalp_engine.ScalpEngine()
        self.scalp_reading: scalp_engine.ScalpReading | None = None

        # Displacement (institutional thrust) — pure, computed per bar off the
        # tape. ORB (opening-range breakout) — stateful, holds the locked range.
        self.displacement_reading: displacement_engine.DisplacementReading | None = None
        self.orb = orb_engine.ORBEngine()
        self.orb_reading: orb_engine.ORBReading | None = None

        # Market-On-Close — the power-hour close play. Stateful (mirrors ScalpEngine):
        # on_chain caches the ATM call/put premium + volume each poll, on_bar runs the
        # premium-behavior reversal / capitulation / heuristic reads.
        self.moc = moc_engine.MocEngine()
        self.moc_reading: moc_engine.MocReading | None = None

        # Intermarket convergence — /ES Level-1 microstructure + SPY/QQQ 0DTE
        # gamma structure, assembled fresh each bar (see _build_intermarket).
        self.es_tracker = intermarket_engine.ESTracker()
        self.es_reading: intermarket_engine.ESReading | None = None
        self.spy_profile: gex_engine.GexProfile | None = None
        self.qqq_profile: gex_engine.GexProfile | None = None
        # Freshest assembled intermarket read, set each bar for the embed/journal.
        self.intermarket: intermarket_engine.IntermarketProfile | None = None
        # /ES cumulative-delta exhaustion read — a leading reversal/veto signal
        # pulled from the stream each bar (None when disabled or the /ES stream is
        # stale, so the gate/analyst cleanly get no CD signal).
        self.cumdelta_reading: cumdelta_engine.CumDeltaReading | None = None
        # Approach/arm (stage 1) — the live anticipatory WATCH (None when disabled,
        # expired, or nothing is being approached). When a reactive entry fires
        # aligned with it, the entry's confidence is pre-loaded. `_arm_ttl` counts
        # bars remaining before the arm expires; `_watch_log` debounces re-posts.
        self.active_arm: ApproachArm | None = None
        self._arm_ttl: int = 0
        self._watch_log: dict[str, float] = {}
        # ES-leads-SPX micro-lead — a streamed /ES thrust ARMS a direction; the
        # gate fires an ES-led candidate the moment SPX confirms. The arm decays
        # over `_es_arm_ttl` bars and is only fed behind the stream staleness gate.
        self.es_lead = es_lead_engine.ESLeadTracker()
        self._es_arm: Direction | None = None
        self._es_arm_ttl: int = 0

        # Accuracy feedback loop
        self.journal = Journal()
        self.tracker = OutcomeTracker()
        self.lessons = LessonStore()
        # Carry any prior learned lessons into this session's analysis.
        self.analyst.set_lessons(self.lessons.load())

        # Macro tape. $VIX (30-day) feeds the macro board + premarket band; $VIX1D
        # (1-day / 0DTE vol) drives the vanna tracker — far more sensitive to the
        # intraday vol crush the rally is built on.
        self.vix_history: deque[float] = deque(maxlen=500)
        self.vix1d_history: deque[float] = deque(maxlen=500)
        self.macro_quotes: dict = {}
        # SPY cumulative volume read at each bar boundary (not async-accumulated),
        # so each bar's volume is the delta over exactly that bar — see bar_worker.
        self._last_spy_cum_volume: float | None = None
        # Freshest spot, fed by both the chain poll and the 30s macro $SPX quote,
        # so the tape doesn't starve when the chain throttle widens.
        self._last_spot: float | None = None
        # Intra-bar extremes, folded from spot samples between bar closes. Without
        # a streaming feed this is how Bar.high/low get real range — see _observe_spot.
        self._bar_high: float = float("-inf")
        self._bar_low: float = float("inf")

        self._alert_log: dict[str, float] = {}
        self._candidate_log: dict[str, float] = {}
        self._last_map_reason: str = ""   # why the GEX map last vetoed (for the log)
        self._premarket_sent_on: dt.date | None = None
        self._moc_brief_sent_on: dt.date | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def setup_hook(self) -> None:
        await self.feed.connect()
        # Warm the tape from today's 1-min history before any bar closes, so a
        # mid-session restart doesn't start blind (wrong session H/L, cold
        # baselines). Best-effort — never block startup on it.
        await self._backfill_state()
        # Seed the scalp EMAs from the backfilled tape so a mid-session restart
        # doesn't print a false cross on the first live bar.
        self.scalp.warm(list(self.state.bars))
        # Rebuild the locked opening range (and suppress an already-broken session)
        # from the backfill, so a restart doesn't re-alert a past ORB breakout.
        self.orb.warm(list(self.state.bars))
        self.add_command(cmd_gex)
        self.add_command(cmd_charm)
        self.add_command(cmd_scalp)
        self.add_command(cmd_disp)
        self.add_command(cmd_orb)
        self.add_command(cmd_moc)
        self.add_command(cmd_scan)
        self.add_command(cmd_stream)
        self.add_command(cmd_flow)
        self.add_command(cmd_inter)
        self.add_command(cmd_stats)
        self.add_command(cmd_review)
        # chain_worker self-paces, so it's a raw task; the rest are tasks.loop
        self._chain_task = asyncio.create_task(self._chain_worker(), name="chain_worker")
        # The streaming feed self-paces its own reconnect loop (raw task too).
        self._stream_task: asyncio.Task | None = None
        if CONFIG.stream.enabled:
            self._stream_task = asyncio.create_task(self.stream.start(), name="stream_worker")
        self.bar_worker.start()
        self.macro_worker.start()
        self.intermarket_worker.start()
        self.premarket_worker.start()
        self.moc_brief_worker.start()
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
        stream_task = getattr(self, "_stream_task", None)
        if stream_task is not None:
            stream_task.cancel()
        await self.stream.aclose()
        await self.feed.close()
        await self.analyst.aclose()
        await super().close()

    def alert_channel(self) -> discord.abc.Messageable | None:
        return self.get_channel(DISCORD_CHANNEL_ID)

    async def _post(self, embed: discord.Embed) -> bool:
        """Send one embed to the alert channel; log (never raise) on failure.

        Returns True if it went out. A delivery problem — channel that won't
        resolve, missing perms, transient network/Discord error — is logged
        loudly and swallowed, so it can neither (a) pass silently as it did
        before (alerts journaled but never posted, with no trace) nor (b) raise
        out of a tasks.loop and kill the worker.
        """
        channel = self.alert_channel()
        if channel is None:
            log.warning(
                "Alert NOT delivered: channel %s did not resolve — bot isn't in "
                "that server, or HELENUS_CHANNEL_ID is wrong/stale. Config is read "
                "once at startup, so restart after editing .env. "
                "(Run scripts/diagnose_channel.py to inspect.)",
                DISCORD_CHANNEL_ID,
            )
            return False
        try:
            await channel.send(embed=embed)
            return True
        except Exception:
            log.exception("Alert send to channel %s failed", DISCORD_CHANNEL_ID)
            return False

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
                    # Maintain the persistent GEX map (wall persistence + price's
                    # position in the envelope + the directional prior) — the primary
                    # frame everything else is scored against. Dormant until enabled.
                    if CONFIG.gexmap.enabled:
                        self.gex_map = self.gexmap_tracker.update(self.profile)
                    # Each chain poll is a fresh spot sample (~15s); fold it into
                    # the forming bar so high/low capture true intra-bar range.
                    self._observe_spot(self.profile.spot)
                    self.vol_profile = flow_engine.build_volume_profile(payload)
                    # Vanna runs off $VIX1D, and is suppressed in the late-day
                    # window where VIX1D ramps mechanically into the close.
                    self.vanna_reading = self.vanna.update(
                        self.vol_profile,
                        list(self.vix1d_history),
                        self._vix1d_eod_distortion(),
                    )
                    # Charm reads the same chain; minutes-to-expiry is the clock
                    # input (charm ∝ 1/T, so this is what makes it ramp into the
                    # afternoon).
                    self.charm_profile = charm_engine.build_charm_profile(
                        payload, charm_engine.minutes_to_expiry(now_et())
                    )
                    # Feed the scalp engine the contract table + ATM-wing premium
                    # series (dual-bleed read + the premium front-run EMAs).
                    self.scalp.on_chain(payload, self.profile.spot)
                    # Point the stream's option subscription at the current ATM
                    # call/put so it tracks the right contracts as spot drifts.
                    if CONFIG.stream.enabled:
                        call_sym, put_sym = schwab_stream.atm_option_symbols(
                            payload, self.profile.spot
                        )
                        await self.stream.set_option_symbols(call_sym, put_sym)
                    # Feed the MOC engine the ATM call/put premium + volume. When
                    # the stream is live the premium comes from real-time ticks
                    # (bar_worker → feed_stream), so skip the poll folding here to
                    # avoid double-counting; otherwise fold the poll marks as before.
                    if not self.stream.is_fresh("options"):
                        self.moc.on_chain(payload, self.profile.spot)
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
        # Freshest spot (macro $SPX quote or chain poll), not just the chain mark.
        spot = self._last_spot if (self._last_spot and self._last_spot > 0) else self.profile.spot
        if spot <= 0:
            return  # no valid spot yet — don't poison the tape / extremes
        self._observe_spot(spot)  # ensure the closing print is in the range
        bar = Bar(
            ts=now_et(),
            open=self.state.bars[-1].close if self.state.bars else spot,
            high=self._bar_high,
            low=self._bar_low,
            close=spot,
            volume=self._bar_volume(),
        )
        # Reset extremes for the next interval (after the close was folded in).
        self._bar_high = float("-inf")
        self._bar_low = float("inf")
        self.state.push_bar(bar)
        self.feed.throttle.report_realized_range(self.state.realized_range_5m())

        # Update the EMA-ignition read off the just-closed bar (every bar, so the
        # EMAs/cross detection stay live even when nothing fires).
        self.scalp_reading = self.scalp.on_bar(
            self.state, self.profile, self.vanna_reading, self.charm_profile
        )
        # Displacement (pure, off the tape) + ORB (stateful range) reads, also
        # every bar so the structure/range stay live between fires.
        self.displacement_reading = displacement_engine.build_displacement(
            self.state, self.profile, self.charm_profile
        )
        self.orb_reading = self.orb.on_bar(
            bar, self.state.vwap(), self.state.volume_ratio()
        )
        # MOC close-play read (premium candles + reversal/capitulation), every bar
        # so the premium EMAs and the window phase stay live between fires. When the
        # stream is live, load this bar's ATM premium from real-time ticks (true
        # intra-bar wicks) instead of the poll-folded marks; else use the poll path.
        if self.stream.is_fresh("options"):
            self.moc.feed_stream(
                self._roll_stream_side("call"), self._roll_stream_side("put")
            )
        self.moc_reading = self.moc.on_bar(self.state, self.profile, now_et())

        # Roll MFE/MAE for every open alert and finalize any that matured.
        for open_alert, outcome in self.tracker.update(spot):
            await self._finalize_outcome(open_alert, outcome)

        # Cheap gate decides whether this bar is worth a Claude call; Claude
        # makes the actual judgment. An active vanna setup, a gated EMA ignition,
        # a sweep, or a level-cross-on-volume can trip it. Most bars don't.
        # /ES cumulative-delta exhaustion read — None when disabled or the stream
        # is stale (clean fallback to today's behavior).
        self.cumdelta_reading = self.stream.cumdelta_reading()
        # ES-leads-SPX: feed the streamed /ES bar-close into the lead tracker and
        # maintain the arm (behind the stream staleness gate; arm decays to None
        # when /ES is stale/disabled, so the poll-only path is unchanged).
        self._update_es_arm()
        # Approach/arm (stage 1): the anticipatory WATCH + the entry pre-load.
        await self._update_arm(
            detect_approach(
                self.state, self.profile, self.charm_profile,
                self.vanna_reading, self.cumdelta_reading,
                self.gex_map if CONFIG.gexmap.enabled else None,
            ) if CONFIG.approach.enabled else None
        )
        candidate = detect_candidate(
            self.state, self.profile, self.vanna_reading, self.charm_profile,
            self.scalp_reading, self.displacement_reading, self.orb_reading,
            self.moc_reading, self.cumdelta_reading, self._es_arm,
        )
        if candidate is None or self._candidate_recently_judged(candidate):
            return
        # GEX-primary pre-Claude gate: the map is consulted FIRST. For a candidate that
        # carries an implied direction, score its location/agreement against the map
        # and SUPPRESS it (skip the Claude call entirely — the strongest, cheapest OFF)
        # when it's into a defended wall / against the prior / no room. The map move
        # triggers (regime flip, CD reversal) are exempt; direction-agnostic triggers
        # fall through to the post-verdict map gate in the analyst. The candidate is NOT
        # marked judged — the veto is structural and cheap to recompute, so it can fire
        # later if the map comes to agree. Dormant until CONFIG.gexmap.enabled.
        if self._map_vetoes(candidate):
            log.info("GEX map OFF (pre-Claude): %s suppressed — %s",
                     candidate.trigger.value, self._last_map_reason)
            return
        # Assemble the intermarket read fresh (pure): ES microstructure from the
        # macro loop + SPY/QQQ structure from the intermarket loop + %-change legs.
        intermarket = intermarket_engine.build_intermarket(
            self.es_reading,
            self.spy_profile,
            self.qqq_profile,
            self.profile,
            self.macro_quotes,
        )
        self.intermarket = intermarket
        sig = await self.analyst.analyze(
            self.state,
            self.profile,
            self.vol_profile,
            self.vanna_reading,
            self.charm_profile,
            self.scalp_reading,
            self.displacement_reading,
            self.orb_reading,
            self.moc_reading,
            intermarket,
            self.macro_quotes,
            candidate,
            self.cumdelta_reading,
            self.active_arm,
            self.gex_map,
        )
        # A transient Claude failure (overload/500) must NOT consume this
        # candidate's cooldown — otherwise one blip silences the trigger+level
        # for the whole window even though no judgment was ever made. Only mark
        # it judged once Claude actually answered (a Signal or a clean no-signal).
        if not self.analyst.last_call_ok:
            return
        self._mark_candidate_judged(candidate)
        if sig is not None:
            await self._dispatch(sig)
            # An aligned entry consumes the arm — the two-stage signal has paid off.
            if self.active_arm is not None and sig.direction == self.active_arm.direction:
                self.active_arm = None

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
        # $VIX1D drives the vanna tracker (see chain_worker / VannaTracker).
        vix1d = (quotes.get("$VIX1D", {}).get("quote") or {}).get("lastPrice")
        if vix1d:
            self.vix1d_history.append(float(vix1d))

        # /ES Level-1 microstructure (futures volume flow + resting bid/ask size).
        # Tracked here on the 30s macro cadence; the interval volume delta needs
        # the stateful tracker (like the SPY proxy / vanna flow).
        # Prefer the streamed /ES Level-1 (real-time bid/ask size + volume) when
        # fresh; else the 30s REST quote. ESTracker reads the same inner-quote keys.
        es_quote = self.stream.es_quote() or (
            (quotes.get(CONFIG.intermarket.es_symbol, {}).get("quote")) or {}
        )
        self.es_reading = self.es_tracker.update(es_quote)

        # $SPX index quote: a spot sample independent of the chain throttle, plus
        # the prior-session close (set once) so "Prior Close" becomes a real level.
        spx_q = quotes.get(UNDERLYING_SYMBOL, {}).get("quote") or {}
        spx_last = spx_q.get("lastPrice")
        if spx_last:
            self._observe_spot(float(spx_last))
        if self.state.prior_close is None:
            spx_close = spx_q.get("closePrice")
            if spx_close:
                self.state.prior_close = float(spx_close)
        # SPY cumulative volume is read at the bar boundary (see _bar_volume),
        # not accumulated here, so each bar's volume aligns to exactly that bar.

    @macro_worker.before_loop
    async def _macro_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=CONFIG.intermarket.intermarket_chain_secs)
    async def intermarket_worker(self) -> None:
        """SPY + QQQ 0DTE gamma structure for the intermarket-convergence read.

        Polls slower than the 15s SPX chain (gamma structure drifts slowly), and
        each leg degrades gracefully — a failed/empty chain just leaves that
        profile stale/None, and the leg falls back to its quote-only directional
        lean downstream."""
        if market_session() != "regular":
            return
        cfg = CONFIG.intermarket
        for symbol, attr in ((cfg.spy_symbol, "spy_profile"), (cfg.qqq_symbol, "qqq_profile")):
            try:
                payload = await self.feed.fetch_0dte_chain_for(symbol)
                setattr(self, attr, gex_engine.build_profile(payload))
            except Exception:
                log.exception("intermarket_worker %s cycle failed", symbol)

    @intermarket_worker.before_loop
    async def _intermarket_ready(self) -> None:
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
        # Mark sent only on a confirmed post, so a delivery failure doesn't
        # silently burn today's briefing.
        if await self._post(embeds.premarket_embed(sig, macro_lines)):
            self._premarket_sent_on = today

    @premarket_worker.before_loop
    async def _premarket_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(
        time=dt.time(
            hour=CONFIG.moc.brief_hour, minute=CONFIG.moc.brief_minute, tzinfo=ET
        )
    )
    async def moc_brief_worker(self) -> None:
        """MOC setup briefing — one per session a few minutes before 3:50 ET."""
        today = now_et().date()
        if self._moc_brief_sent_on == today or market_session() != "regular":
            return
        if self.moc_reading is None or self.profile is None:
            return
        try:
            context = self._moc_context()
            sig, lines = await self.analyst.moc_briefing(context)
        except Exception:
            log.exception("MOC briefing routine failed")
            return
        # Posted directly (not via _dispatch), so it's never journaled/graded —
        # it's a bias call, not an intraday entry, like the pre-market briefing.
        if await self._post(embeds.moc_briefing_embed(sig, lines)):
            self._moc_brief_sent_on = today

    @moc_brief_worker.before_loop
    async def _moc_brief_ready(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(
        time=dt.time(
            hour=CONFIG.feedback.review_hour,
            minute=CONFIG.feedback.review_minute,
            tzinfo=ET,
        )
    )
    async def review_worker(self) -> None:
        """Daily accuracy review at the market close — Claude reads the graded
        journal for patterns."""
        review = await self._run_review()
        if review is None:
            return
        await self._post(embeds.review_embed(review, self.journal.stats()))

    @review_worker.before_loop
    async def _review_ready(self) -> None:
        await self.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _vix1d_eod_distortion(self) -> bool:
        """True inside the late-day window where $VIX1D's measurement window
        mechanically shrinks and ramps into the close — there the vanna trigger is
        suppressed (the ramp is structural, not a real vol move)."""
        cfg = CONFIG.flow
        return now_et().time() >= dt.time(cfg.vix1d_distort_hour, cfg.vix1d_distort_minute)

    def _roll_stream_side(self, role: str):
        """Finalize the streamed ATM call/put premium candle for this bar (or None
        when that side isn't subscribed yet) — fed to MocEngine.feed_stream."""
        contract = self.stream.side(role)
        return contract.roll() if contract is not None else None

    def _observe_spot(self, spot: float) -> None:
        """Record a fresh spot sample. Always updates the freshest-spot pointer;
        only folds into the forming bar's high/low during the regular session, so
        premarket prints don't bleed into the first regular bar's range."""
        if spot <= 0:
            return
        self._last_spot = spot
        if market_session() != "regular":
            return
        self._bar_high = max(self._bar_high, spot)
        self._bar_low = min(self._bar_low, spot)

    def _bar_volume(self) -> float:
        """Per-bar SPY-proxy volume: the cumulative-volume delta since the last
        bar close, read at the boundary. Aligns volume to the bar instead of the
        async ±30s accumulation the macro loop used to do."""
        # Prefer the streamed SPY 1-min bar volume (authoritative) when it's fresh;
        # else fall back to the REST cumulative-volume delta read at the boundary.
        streamed_vol = self.stream.spy_minute_volume()
        if streamed_vol is not None:
            return streamed_vol
        spy_cum = (self.macro_quotes.get(PROXY_SYMBOL, {}).get("quote") or {}).get("totalVolume")
        if spy_cum is None:
            return 0.0
        spy_cum = float(spy_cum)
        prev = self._last_spy_cum_volume
        self._last_spy_cum_volume = spy_cum
        # First reading (or a daily cumulative reset) has no valid delta.
        return max(0.0, spy_cum - prev) if prev is not None else 0.0

    async def _backfill_state(self) -> None:
        """Seed MarketState from today's 1-min history: $SPX OHLC for price (and
        session H/L + trend), SPY per-minute volume for the baseline. Best-effort
        and idempotent-on-restart; never raises into startup."""
        if market_session() == "closed":
            return
        try:
            today = now_et().date()
            start = dt.datetime.combine(today, dt.time(9, 30), tzinfo=ET)
            end = now_et()
            if end <= start:
                return  # premarket — nothing intraday to backfill yet
            spx = await self.feed.fetch_intraday_candles(UNDERLYING_SYMBOL, start, end)
            spy = await self.feed.fetch_intraday_candles(PROXY_SYMBOL, start, end)
            bars = _candles_to_bars(spx, spy)
            for b in bars:
                self.state.push_bar(b)
            if bars:
                self._last_spot = bars[-1].close
                log.info(
                    "Backfilled %d bars (session H/L %.2f / %.2f)",
                    len(bars), self.state.session_high, self.state.session_low,
                )
        except Exception:
            log.exception("state backfill failed — starting with a cold tape")

    @staticmethod
    def _candidate_key(candidate) -> str:
        """Throttle key: trigger+level, so a level price pins (e.g. a GEX wall it
        hovers on through a range session) is judged once per window, not every
        bar it keeps tagging. Vanna/charm candidates carry no level — per trigger.
        """
        lvl = f"{candidate.level.price:.0f}" if candidate.level else "na"
        return f"{candidate.trigger.value}:{lvl}"

    def _candidate_recently_judged(self, candidate) -> bool:
        """True if this trigger+level already got a Claude verdict recently.

        Read-only: it does NOT record the candidate. The cooldown is committed
        separately (`_mark_candidate_judged`) only after Claude actually answers,
        so a failed call doesn't consume the window — see bar_worker.
        """
        now = asyncio.get_running_loop().time()
        return now - self._candidate_log.get(self._candidate_key(candidate), -1e9) < CANDIDATE_COOLDOWN_SECS

    def _mark_candidate_judged(self, candidate) -> None:
        """Start this candidate's cooldown — called only once Claude returned a
        verdict (signal or clean no-signal), never on a transient API failure."""
        self._candidate_log[self._candidate_key(candidate)] = asyncio.get_running_loop().time()

    def _update_es_arm(self) -> None:
        """Feed the streamed /ES bar-close into the lead tracker and maintain the
        ES arm: a qualified /ES thrust (re)arms a direction for `arm_ttl_bars`; each
        bar without one ticks the TTL down and clears it at zero. Only fed when the
        /ES stream is fresh — otherwise the arm decays and the poll-only path is
        unchanged."""
        if not (CONFIG.es_lead.enabled and self.stream.is_fresh("es")):
            if self._es_arm is not None:
                self._es_arm_ttl -= 1
                if self._es_arm_ttl <= 0:
                    self._es_arm = None
            return
        es_last = self.stream.es_quote().get("lastPrice")
        if es_last is not None:
            self.es_lead.observe(float(es_last))
        direction = self.es_lead.reading().break_direction
        if direction is not None:
            self._es_arm = direction
            self._es_arm_ttl = CONFIG.es_lead.arm_ttl_bars
        elif self._es_arm is not None:
            self._es_arm_ttl -= 1
            if self._es_arm_ttl <= 0:
                self._es_arm = None

    def _candidate_direction(self, candidate: Candidate) -> Direction | None:
        """The implied trade direction a candidate carries at gate time, where one
        exists — so the map can score it BEFORE Claude. Direction-agnostic triggers
        (level cross/rejection/sweep/range expansion) return None and are gated
        post-verdict once Claude picks the direction."""
        t = candidate.trigger
        if t is TriggerType.EMA_IGNITION and self.scalp_reading is not None:
            return self.scalp_reading.direction
        if t is TriggerType.DISPLACEMENT and self.displacement_reading is not None:
            return self.displacement_reading.direction
        if t is TriggerType.ORB_BREAKOUT and self.orb_reading is not None:
            return self.orb_reading.direction
        if t is TriggerType.VANNA_RALLY:
            return Direction.BULLISH
        if t is TriggerType.PUT_FLOW:
            return Direction.BEARISH
        if t is TriggerType.FLOW_INFLECTION and self.vanna_reading is not None:
            if self.vanna_reading.inflection_active:
                return Direction.BULLISH
            if self.vanna_reading.inflection_bearish:
                return Direction.BEARISH
        if t is TriggerType.ES_LEAD:
            return self._es_arm
        return None

    def _cd_absorption(self, direction: Direction) -> bool:
        """Is there an aligned CD-divergence absorption read in `direction`? Feeds the
        map's wall-defense override (a defended wall being eaten — the break agrees)."""
        cd = self.cumdelta_reading
        if cd is None:
            return False
        return (
            (direction is Direction.BULLISH and cd.bullish_absorption)
            or (direction is Direction.BEARISH and cd.bearish_absorption)
        )

    def _map_vetoes(self, candidate: Candidate) -> bool:
        """Pre-Claude map gate: True when the persistent GEX map turns this candidate
        OFF (into a defended wall / against the prior / no room). No-op unless
        CONFIG.gexmap.enabled and the candidate carries an implied direction; the map
        move triggers are exempt. Records the reason for the log line."""
        self._last_map_reason = ""
        if not CONFIG.gexmap.enabled or self.gex_map is None:
            return False
        if candidate.trigger in gexmap_engine.MAP_EXEMPT_TRIGGERS:
            return False
        direction = self._candidate_direction(candidate)
        if direction is None:
            return False
        assessment = self.gex_map.gate(
            direction,
            is_momentum=gexmap_engine.is_momentum_trigger(candidate.trigger),
            absorption=self._cd_absorption(direction),
        )
        if assessment.is_off:
            self._last_map_reason = "; ".join(assessment.reasons)
            return True
        return False

    async def _update_arm(self, arm: ApproachArm | None) -> None:
        """Stage-1 arm lifecycle: tick down the live arm's TTL and expire it; then
        (re)arm on a fresh approach, posting a WATCH heads-up on a NEW arm only
        (debounced via `_watch_log`). The WATCH is posted directly — like the
        pre-market / MOC briefings — so it is never journaled or graded as an entry."""
        if self.active_arm is not None:
            self._arm_ttl -= 1
            if self._arm_ttl <= 0:
                self.active_arm = None
        if arm is None:
            return
        is_new = (
            self.active_arm is None
            or self.active_arm.level.price != arm.level.price
            or self.active_arm.direction != arm.direction
        )
        self.active_arm = arm
        self._arm_ttl = CONFIG.approach.arm_ttl_bars
        if not is_new:
            return
        key = f"WATCH:{arm.level.price:.0f}:{arm.direction.value}"
        now = asyncio.get_running_loop().time()
        if now - self._watch_log.get(key, -1e9) < ALERT_COOLDOWN_SECS:
            return
        self._watch_log[key] = now
        await self._post(embeds.watch_embed(arm, self.profile, self.charm_profile))

    def _record_alert(self, sig: Signal) -> None:
        """Open MFE/MAE tracking for an intraday alert and journal it."""
        if sig.trigger is TriggerType.PREMARKET_SETUP:
            return  # a briefing, not an intraday entry — not price-graded
        p, vp, vn, ch = self.profile, self.vol_profile, self.vanna_reading, self.charm_profile
        im = self.intermarket
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
            "charm_bias": ch.bias if ch else None,
            "charm_intensity": ch.intensity if ch else None,
            "vanna_active": vn.active if vn else None,
            "vanna_label": vn.label if vn else None,
            "otm_call_put_ratio": round(vp.otm_call_put_ratio, 2) if vp else None,
            "vol_above_spot": round(vp.above_spot_vol) if vp else None,
            "vol_below_spot": round(vp.below_spot_vol) if vp else None,
            # Intermarket convergence — so the feedback loop / lessons can learn
            # whether alignment actually predicted accuracy.
            "intermarket_alignment": im.alignment(sig.direction) if im else None,
            "qqq_dir": im.qqq.confirms_label if im and im.qqq else None,
            "spy_dir": im.spy.confirms_label if im and im.spy else None,
            "es_imbalance": im.es.imbalance if im and im.es else None,
            "es_volume_flow": round(im.es.volume_flow) if im and im.es else None,
            # Whether this entry fired aligned with a live approach arm (Task 2) — so
            # the review/lessons loop can learn if pre-armed (earlier) entries grade
            # better. The entry's own mfe_pts is already MFE-from-entry.
            "pre_armed": (
                self.active_arm.direction.value
                if self.active_arm is not None and self.active_arm.direction == sig.direction
                else None
            ),
        }
        # GEX-map measurement — grade by the cell price occupied, the position_state,
        # and whether the verdict AGREED with the structural prior. This is how the
        # journal validates that map-first ranking earns its place: does
        # "agrees-with-prior + room" actually beat "against" on MFE / early-reversal?
        gm = self.gex_map
        if gm is not None:
            assessment = gm.gate(
                sig.direction,
                is_momentum=gexmap_engine.is_momentum_trigger(sig.trigger),
                absorption=self._cd_absorption(sig.direction),
            )
            context.update(
                {
                    "gex_cell": gm.cell,
                    "gex_position_state": gm.position_state,
                    "gex_prior_behavior": gm.prior.expected_behavior,
                    "prior_direction": (
                        gm.prior.favored_direction.value
                        if gm.prior.favored_direction else None
                    ),
                    "prior_agreement": gm.prior.agreement(sig.direction),
                    "gex_map_verdict": assessment.verdict,   # PROMOTE (agrees+room) | OK
                }
            )
        # EMA-ignition scalp context — so the feedback loop / lessons can learn
        # whether the gated 5/9 cross (and its front-run/divergence boosters)
        # actually predicted accuracy. Only present on an EMA_IGNITION alert.
        sc = sig.scalp
        if sc is not None:
            tc = sc.target_contract
            context.update(
                {
                    "scalp_cross_type": sc.cross_type,
                    "scalp_ema_stack": sc.ema_stack,
                    "scalp_slow_grind": sc.slow_grind,
                    "scalp_front_run": sc.front_run,
                    "scalp_premium_divergence": sc.premium_divergence,
                    "scalp_vanna_headwind": sc.vanna_headwind,
                    "scalp_chop_count": sc.chop_count,
                    "scalp_dual_bleed": sc.dual_bleed,
                    "scalp_room_pts": round(sc.room_to_level_pts, 2)
                    if sc.room_to_level_pts != float("inf") else None,
                    "scalp_target_strike": f"{tc.strike:.0f}{'C' if tc.side == 'call' else 'P'}"
                    if tc else None,
                    "scalp_entry_premium": round(tc.premium, 2) if tc else None,
                    "scalp_premium_target": round(sc.premium_target, 2)
                    if sc.premium_target is not None else None,
                }
            )
        # Displacement / ORB context — so the feedback loop can learn whether the
        # pillars / fakeout filters actually predicted accuracy.
        dr = self.displacement_reading
        if sig.trigger is TriggerType.DISPLACEMENT and dr is not None:
            context.update(
                {
                    "disp_body_pts": dr.body_pts,
                    "disp_body_frac": dr.body_frac,
                    "disp_vol_ratio": dr.vol_ratio,
                    "disp_fvg_zone": [dr.fvg_low, dr.fvg_high]
                    if dr.fvg_low is not None else None,
                    "disp_mss_level": dr.mss_level,
                    "disp_fvg": dr.fvg,
                    "disp_mss": dr.mss,
                    "disp_swept": dr.swept,
                    "disp_midpoint": dr.midpoint,
                    "disp_holding_mid": dr.holding_above_mid,
                    "disp_trend": dr.trend_direction.value if dr.trend_direction else None,
                }
            )
        orb = self.orb_reading
        if sig.trigger is TriggerType.ORB_BREAKOUT and orb is not None:
            context.update(
                {
                    "orb_range_pts": orb.range_pts,
                    "orb_entry": orb.entry,
                    "orb_targets": orb.targets,
                    "orb_stop": orb.stop,
                    "orb_volume_ok": orb.volume_ok,
                    "orb_vwap_ok": orb.vwap_ok,
                }
            )
        # MOC context — the LOGGED PATTERNS the review/lessons loop learns from
        # (the close-play patterns drift, so this is what makes them analyzable).
        mr = self.moc_reading
        if sig.trigger in (TriggerType.MOC_REVERSAL, TriggerType.CAPITULATION) and mr is not None:
            context.update(
                {
                    "moc_phase": mr.phase,
                    "moc_minutes_to_close": mr.minutes_to_close,
                    "moc_heuristic_color": mr.heuristic_color,
                    "moc_heuristic_bias": mr.heuristic_bias.value if mr.heuristic_bias else None,
                    "moc_gex_state": mr.gex_state,
                    "moc_basing_side": mr.basing_side,
                    "moc_volume_surge_ratio": mr.volume_surge_ratio,
                    "moc_call_volume": mr.call_volume,
                    "moc_put_volume": mr.put_volume,
                    "moc_capitulation": mr.capitulation,
                    "moc_cap_side": mr.cap_side,
                    "moc_cap_wick_frac": mr.cap_wick_frac,
                }
            )
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

    def _moc_context(self) -> dict:
        """Compact close-window state for the MOC briefing (pure; no I/O)."""
        m = self.moc_reading
        p = self.profile
        ctx: dict = {
            "spot": round(p.spot, 2) if p else None,
            "phase": m.phase if m else None,
            "minutes_to_close": m.minutes_to_close if m else None,
            "gex_state": m.gex_state if m else None,
            "nearest_wall": m.nearest_wall if m else None,
            "regime": p.regime if p else None,
            "zero_gamma": round(p.zero_gamma, 1) if p and p.zero_gamma is not None else None,
        }
        if m is not None:
            if m.heuristic_bias is not None:
                ctx["heuristic"] = {
                    "candle_color": m.heuristic_color,
                    "bias": m.heuristic_bias.value,
                }
            ctx["reversal"] = {
                "basing_side": m.basing_side,
                "direction": m.reversal_direction.value if m.reversal_direction else None,
                "volume_surge_ratio": m.volume_surge_ratio,
                "call_premium": m.call_premium,
                "put_premium": m.put_premium,
                "call_volume": m.call_volume,
                "put_volume": m.put_volume,
            }
            if m.capitulation:
                ctx["capitulation"] = {
                    "side": m.cap_side,
                    "wick_high": m.cap_high,
                    "close": m.cap_close,
                }
        return ctx

    async def _dispatch(self, sig: Signal) -> None:
        """Cooldown-gated alert post."""
        key = f"{sig.trigger.value}:{sig.level.price if sig.level else 'na'}:{sig.direction.value}"
        now = asyncio.get_running_loop().time()
        if now - self._alert_log.get(key, -1e9) < ALERT_COOLDOWN_SECS:
            return
        self._alert_log[key] = now
        # Open MFE/MAE tracking before posting — grade it regardless of Discord.
        self._record_alert(sig)
        posted = await self._post(
            embeds.signal_embed(
                sig, self.profile, self.vol_profile, self.vanna_reading,
                self.charm_profile, self.intermarket, sig.scalp,
                self.displacement_reading, self.orb_reading, self.moc_reading,
            )
        )
        # A flow-driven call (vanna rally or put-flow pressure) is about the
        # volume distribution — attach the breakdown.
        flow_triggers = (TriggerType.VANNA_RALLY, TriggerType.PUT_FLOW, TriggerType.FLOW_INFLECTION)
        if posted and sig.trigger in flow_triggers and self.vol_profile is not None:
            await self._post(
                embeds.volume_profile_embed(self.vol_profile, self.vanna_reading)
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


@commands.command(name="charm")
async def cmd_charm(ctx: commands.Context) -> None:
    """!charm — post the current OTM-wing charm (delta-decay) structure board."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.charm_profile is None:
        await ctx.send("No charm snapshot yet — engine warming up.")
        return
    await ctx.send(embed=embeds.charm_snapshot_embed(bot.charm_profile))


@commands.command(name="scalp")
async def cmd_scalp(ctx: commands.Context) -> None:
    """!scalp — post the current EMA-ignition scalp board (5/9/200 EMAs + gates)."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.scalp_reading is None:
        await ctx.send("No scalp read yet — engine warming up.")
        return
    await ctx.send(embed=embeds.scalp_snapshot_embed(bot.scalp_reading))


@commands.command(name="disp")
async def cmd_disp(ctx: commands.Context) -> None:
    """!disp — post the current displacement (institutional thrust) read."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.displacement_reading is None:
        await ctx.send("No displacement read yet — engine warming up.")
        return
    await ctx.send(embed=embeds.displacement_snapshot_embed(bot.displacement_reading))


@commands.command(name="orb")
async def cmd_orb(ctx: commands.Context) -> None:
    """!orb — post the current opening-range-breakout board."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.orb_reading is None:
        await ctx.send("No ORB read yet — engine warming up (or pre-open).")
        return
    await ctx.send(embed=embeds.orb_snapshot_embed(bot.orb_reading))


@commands.command(name="moc")
async def cmd_moc(ctx: commands.Context) -> None:
    """!moc — post the current Market-On-Close (power-hour close play) board."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.moc_reading is None:
        await ctx.send("No MOC read yet — engine warming up.")
        return
    await ctx.send(embed=embeds.moc_snapshot_embed(bot.moc_reading))


@commands.command(name="stream")
async def cmd_stream(ctx: commands.Context) -> None:
    """!stream — post the real-time websocket feed status (subs + freshness)."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    await ctx.send(embed=embeds.stream_status_embed(bot.stream.status()))


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
        f"Session H/L: `{st.session_high:.0f}` (×{st.high_retests}) / "
        f"`{st.session_low:.0f}` (×{st.low_retests}) re-tests"
        if st.bars else "Session H/L: `warming up`",
        "Levels: " + ", ".join(f"`{lv.label} {lv.price:.0f}`" for lv in levels[:6]),
    ]
    gm = bot.gex_map
    if gm is not None:
        fav = gm.prior.favored_direction
        lines.append(
            f"GEX map: `{gm.position_state}` in `{gm.cell}` | "
            f"prior `{gm.prior.expected_behavior}` "
            f"favors `{fav.value if fav else 'NONE'}`"
        )
    await ctx.send("\n".join(lines))


@commands.command(name="flow")
async def cmd_flow(ctx: commands.Context) -> None:
    """!flow — post the 0DTE options-volume summary + vanna read."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    if bot.vol_profile is None:
        await ctx.send("No 0DTE volume snapshot yet — engine warming up.")
        return
    await ctx.send(
        embed=embeds.volume_profile_embed(bot.vol_profile, bot.vanna_reading)
    )


@commands.command(name="inter")
async def cmd_inter(ctx: commands.Context) -> None:
    """!inter — post the intermarket board (/ES microstructure + SPY/QQQ structure)."""
    bot: HelenusBot = ctx.bot  # type: ignore[assignment]
    im = intermarket_engine.build_intermarket(
        bot.es_reading, bot.spy_profile, bot.qqq_profile, bot.profile, bot.macro_quotes
    )
    await ctx.send(embed=embeds.intermarket_embed(im))


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
        f"Early-reversal rate **{s.get('early_reversal_rate', 0)}%** "
        f"(avg first-bars MAE `{s.get('avg_early_mae_pts', 0)}` pts)",
    ]
    for trig, d in s.get("by_trigger", {}).items():
        er = f", {d['early_reversal']} early-rev" if d.get("early_reversal") else ""
        lines.append(f"`{trig}`: {d['ACCURATE']}/{d['n']} accurate{er}")
    # GEX-map validation: does agreeing with the structural prior actually pay? Show
    # the agrees-vs-against split (accuracy, avg MFE, early-reversal) when it's present.
    by_pa = s.get("by_prior_agreement", {})
    if by_pa:
        lines.append("**GEX prior agreement:**")
        for label in ("AGREES", "AGAINST", "NEUTRAL"):
            d = by_pa.get(label)
            if d:
                lines.append(
                    f"`{label}`: {d['accuracy_pct']}% acc, MFE `{d['avg_mfe_pts']}`, "
                    f"early-rev `{d['early_reversal_rate']}%` (n={d['n']})"
                )
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
