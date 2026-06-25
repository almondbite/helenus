"""
Real-time market data over the schwab-py StreamClient (websocket).

Helenus is otherwise REST-poll driven (see data/schwab_feed.py). This module adds
a single websocket connection as an **additive, flag-gated** layer: when it's live
the engines prefer streamed data, and when it's disabled or stale they fall back to
the existing poll path, so nothing breaks if streaming is unavailable.

What Schwab actually streams (and what we wire — see CONFIG.stream):
  * level_one_option  — the ATM call/put → true per-contract premium ticks, folded
    into real 1-min premium OHLC *with wicks* (the MOC capitulation candle and the
    reversal can't see a 1-min wick at 15s poll sampling).
  * level_one_futures — /ES → real-time bid/ask size + volume (intermarket
    microstructure), mapped into the inner-`quote` shape engine.intermarket reads.
  * chart_equity      — SPY → authoritative 1-min OHLCV (the tape's per-min volume).

NOTE: the $SPX *index* is not a streamable level-one symbol on Schwab (indices are
screener-only), so the index price tape stays poll-based; streaming targets option
premium, /ES, and SPY only.

Design: the pure aggregation/mapping pieces (`StreamedContract`, `_es_stream_to_quote`,
`_diff_symbols`, `atm_option_symbols`) are split out and unit-tested without a socket;
`SchwabStream` is the thin async shell that owns the connection, the handlers, and the
resilient reconnect loop. schwab-py relabels each message's numeric field keys to the
field-enum NAMES (e.g. "MARK", "BID_SIZE", "TOTAL_VOLUME", "VOLUME"), and level-one
messages are field-DELTAS — so every read here is defensive (a tick may omit a field).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from helenus.config import CONFIG, StreamConfig
from helenus.engine.cumdelta import CumDeltaReading, CumDeltaTracker

log = logging.getLogger("helenus.stream")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without a socket)
# --------------------------------------------------------------------------- #

def _f(d: dict[str, Any], *keys: str) -> float:
    """First present, numeric value among `keys`, else NaN. Mirrors
    intermarket._num but returns NaN (so callers can distinguish 'absent')."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return float("nan")


def _es_stream_to_quote(raw: dict[str, Any]) -> dict[str, Any]:
    """Map merged streamed level-one-futures fields into the inner-`quote` keys
    `intermarket.ESTracker.update` reads. Only finite values are included, so a
    missing field falls through to ESTracker's 0.0 default rather than poisoning
    it with NaN. `netPercentChange` prefers the streamed percent, else derives it
    from last vs the prior settlement."""
    out: dict[str, Any] = {}

    def put(key: str, val: float) -> None:
        if val == val:  # not NaN
            out[key] = val

    last = _f(raw, "LAST_PRICE", "MARK", "CLOSE_PRICE")
    put("lastPrice", last)
    pct = _f(raw, "FUTURE_CHANGE_PERCENT", "NET_PERCENT_CHANGE")
    settle = _f(raw, "FUTURE_SETTLEMENT_PRICE", "CLOSE_PRICE")
    if pct != pct and settle == settle and settle > 0 and last == last:
        pct = (last / settle - 1.0) * 100.0
    put("netPercentChange", pct)
    put("bidSize", _f(raw, "BID_SIZE"))
    put("askSize", _f(raw, "ASK_SIZE"))
    put("totalVolume", _f(raw, "TOTAL_VOLUME"))
    return out


def _diff_symbols(current: set[str], want: set[str]) -> tuple[set[str], set[str]]:
    """(to_add, to_remove) for moving a subscription set from `current` to `want`."""
    return want - current, current - want


def atm_option_symbols(payload: dict[str, Any], spot: float) -> tuple[str | None, str | None]:
    """The nearest-strike contract `symbol` per side from a raw 0DTE chain payload
    — the ATM call/put to subscribe to. Pure; no greeks filtering (we only need the
    symbol string the streamer expects, e.g. 'SPXW  260623C07520000')."""
    best: dict[str, tuple[float, str | None]] = {
        "call": (float("inf"), None),
        "put": (float("inf"), None),
    }
    for side, key in (("call", "callExpDateMap"), ("put", "putExpDateMap")):
        for _exp, strikes in (payload.get(key, {}) or {}).items():
            for _sk, contracts in strikes.items():
                for c in contracts:
                    try:
                        strike = float(c.get("strikePrice"))
                    except (TypeError, ValueError):
                        continue
                    sym = c.get("symbol")
                    if not sym or spot <= 0:
                        continue
                    dist = abs(strike - spot)
                    if dist < best[side][0]:
                        best[side] = (dist, sym)
    return best["call"][1], best["put"][1]


class StreamedContract:
    """Per option-symbol premium aggregator. Folds level-one ticks into a forming
    1-min premium candle (real wicks) + the fresh interval volume + a marks buffer,
    mirroring MocEngine._PremiumSide.observe. `roll()` finalizes and resets for the
    next bar, returning exactly what MocEngine.feed_stream wants."""

    def __init__(self, symbol: str, history: int = 60) -> None:
        self.symbol = symbol
        self.mark: float | None = None
        self.bid: float = 0.0
        self.ask: float = 0.0
        self.last: float | None = None
        self.cum_volume: float = 0.0
        self.updated: float = 0.0
        # Forming candle + per-bar pending series / interval volume.
        self._o: float | None = None
        self._h: float = float("-inf")
        self._l: float = float("inf")
        self._c: float | None = None
        self._pending: list[float] = []
        self.interval_vol: float = 0.0
        self._last_cum_vol: float | None = None
        self._recent: deque[float] = deque(maxlen=history)   # for the board / display

    def observe(
        self,
        mark: float,
        bid: float = float("nan"),
        ask: float = float("nan"),
        last: float = float("nan"),
        cum_volume: float = float("nan"),
        now: float | None = None,
    ) -> None:
        if mark == mark and mark > 0:        # not NaN, positive
            self.mark = mark
            self._recent.append(mark)
            self._pending.append(mark)
            if self._o is None:
                self._o = mark
            self._h = max(self._h, mark)
            self._l = min(self._l, mark)
            self._c = mark
        if bid == bid:
            self.bid = bid
        if ask == ask:
            self.ask = ask
        if last == last:
            self.last = last
        if cum_volume == cum_volume:
            if self._last_cum_vol is not None:
                self.interval_vol += max(0.0, cum_volume - self._last_cum_vol)
            self._last_cum_vol = cum_volume
            self.cum_volume = cum_volume
        self.updated = now if now is not None else time.monotonic()

    def roll(self) -> tuple[tuple[float, float, float, float] | None, list[float], float]:
        """Finalize the bar: (candle | None, fresh marks, interval volume), and
        reset the forming candle / pending series / interval volume."""
        candle = (
            (self._o, self._h, self._l, self._c)
            if self._o is not None and self._c is not None else None
        )
        marks = self._pending
        iv = self.interval_vol
        self._o = self._c = None
        self._h, self._l = float("-inf"), float("inf")
        self._pending = []
        self.interval_vol = 0.0
        return candle, marks, iv


# --------------------------------------------------------------------------- #
# The async streaming shell
# --------------------------------------------------------------------------- #

class SchwabStream:
    """Owns the StreamClient websocket: login, handler registration, the dynamic
    ATM-option subscription, and a resilient reconnect loop. Handlers are sync and
    only mutate freshest-state; the bot pulls that state each bar/poll and prefers
    it over the REST path when fresh."""

    def __init__(self, feed: Any, cfg: StreamConfig | None = None) -> None:
        self.feed = feed
        self.cfg = cfg or CONFIG.stream
        self._es_symbol = CONFIG.intermarket.es_symbol
        self._spy_symbol = CONFIG.intermarket.spy_symbol

        self._stream: Any = None
        self._lock = asyncio.Lock()       # subs are not thread-safe — serialize them
        self._stopped = False
        self._connected = False

        # Freshest streamed state.
        self.options: dict[str, StreamedContract] = {}
        self._roles: dict[str, str | None] = {"call": None, "put": None}
        self._opt_active: set[str] = set()
        self._es_raw: dict[str, Any] = {}
        self._spy_minute_vol: float | None = None
        self._last: dict[str, float] = {"options": 0.0, "es": 0.0, "spy": 0.0}
        # /ES cumulative-delta exhaustion read, folded per futures tick (executed
        # flow → signed cumulative delta). Flag-gated; pulled per bar by the bot
        # behind the same staleness gate as the rest of the streamed state.
        self._cumdelta = CumDeltaTracker()

    # -- lifecycle --------------------------------------------------------- #

    async def start(self) -> None:
        """Construct the StreamClient, register handlers once, then run the
        resilient login→subscribe→handle_message loop with reconnect backoff."""
        try:
            from schwab.streaming import StreamClient
        except Exception:
            log.exception("schwab.streaming import failed — streaming disabled")
            return
        try:
            account_id = await self._account_id()
            self._stream = StreamClient(self.feed._client, account_id=account_id)
            # Handlers are registered ONCE (re-registering on reconnect would
            # double-fire them and double-count volume).
            self._stream.add_level_one_option_handler(self._on_option)
            self._stream.add_level_one_futures_handler(self._on_futures)
            self._stream.add_chart_equity_handler(self._on_chart)
        except Exception:
            log.exception("StreamClient construction failed — streaming disabled")
            return

        backoff = self.cfg.reconnect_backoff_secs
        while not self._stopped:
            try:
                await self._login_and_subscribe()
                backoff = self.cfg.reconnect_backoff_secs   # reset on a clean connect
                while not self._stopped:
                    await self._stream.handle_message()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                if self._stopped:
                    break
                log.exception("stream loop error — reconnecting in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, self.cfg.max_backoff_secs)

    async def _account_id(self) -> str | None:
        """Resolve an account number for the StreamClient. Market-data streaming
        doesn't strictly need it, so a failure just lets schwab-py default."""
        try:
            resp = await self.feed._client.get_account_numbers()
            resp.raise_for_status()
            data = resp.json() or []
            return data[0].get("accountNumber") if data else None
        except Exception:
            log.warning("could not resolve account number; StreamClient will default", exc_info=True)
            return None

    async def _login_and_subscribe(self) -> None:
        await self._stream.login()
        self._connected = True
        async with self._lock:
            if self.cfg.subscribe_es:
                await self._stream.level_one_futures_subs([self._es_symbol])
            if self.cfg.subscribe_spy:
                await self._stream.chart_equity_subs([self._spy_symbol])
            syms = [s for s in self._roles.values() if s]
            if self.cfg.subscribe_options and syms:
                await self._stream.level_one_option_subs(syms)
                self._opt_active = set(syms)
        log.info("stream connected (es=%s spy=%s opts=%s)",
                 self.cfg.subscribe_es, self.cfg.subscribe_spy, sorted(self._opt_active))

    async def aclose(self) -> None:
        self._stopped = True
        if self._stream is not None:
            try:
                await self._stream.logout()
            except Exception:
                log.debug("stream logout failed", exc_info=True)

    # -- dynamic option subscription (called by the bot each chain poll) --- #

    async def set_option_symbols(self, call: str | None, put: str | None) -> None:
        """Track the current ATM call/put symbols and move the live option
        subscription to match. Records the roles even when not yet connected, so
        the next (re)subscribe picks them up."""
        self._roles = {"call": call, "put": put}
        if not (self.cfg.enabled and self.cfg.subscribe_options and self._connected and self._stream):
            return
        want = {s for s in (call, put) if s}
        add, remove = _diff_symbols(self._opt_active, want)
        if not add and not remove:
            return
        async with self._lock:
            try:
                if not self._opt_active and want:
                    await self._stream.level_one_option_subs(list(want))
                else:
                    if add:
                        await self._stream.level_one_option_add(list(add))
                    if remove:
                        await self._stream.level_one_option_unsubs(list(remove))
            except Exception:
                log.exception("option (un)subscribe failed")
                return
        for s in add:
            self.options.setdefault(s, StreamedContract(s))
        for s in remove:
            self.options.pop(s, None)
        self._opt_active = want

    # -- handlers (sync, fast — just update freshest-state) ---------------- #

    def _on_option(self, msg: dict[str, Any]) -> None:
        now = time.monotonic()
        for item in msg.get("content", []) or []:
            c = self.options.get(item.get("key"))
            if c is None:
                continue
            c.observe(
                _f(item, "MARK", "LAST_PRICE"),
                _f(item, "BID_PRICE"),
                _f(item, "ASK_PRICE"),
                _f(item, "LAST_PRICE"),
                _f(item, "TOTAL_VOLUME"),
                now=now,
            )
        self._last["options"] = now

    def _on_futures(self, msg: dict[str, Any]) -> None:
        now = time.monotonic()
        for item in msg.get("content", []) or []:
            if item.get("key") != self._es_symbol:
                continue
            for k, v in item.items():
                if k != "key":
                    self._es_raw[k] = v      # merge field-deltas into a running quote
            self._last["es"] = now
            # Fold the (merged) tick into the cumulative-delta tracker — executed
            # flow signed by the quote/tick rule. The intermarket path above is
            # untouched; this is purely additive and only runs when flag-gated on.
            if CONFIG.cumdelta.enabled:
                self._cumdelta.observe(
                    _f(self._es_raw, "LAST_PRICE", "MARK", "CLOSE_PRICE"),
                    _f(self._es_raw, "BID_PRICE"),
                    _f(self._es_raw, "ASK_PRICE"),
                    _f(self._es_raw, "TOTAL_VOLUME"),
                    now=now,
                )

    def _on_chart(self, msg: dict[str, Any]) -> None:
        now = time.monotonic()
        for item in msg.get("content", []) or []:
            if item.get("key") != self._spy_symbol:
                continue
            vol = _f(item, "VOLUME")
            if vol == vol:
                self._spy_minute_vol = vol
            self._last["spy"] = now

    # -- accessors (pulled by the bot; freshness-gated) -------------------- #

    def is_fresh(self, which: str) -> bool:
        if not (self.cfg.enabled and self._connected):
            return False
        stamp = self._last.get(which, 0.0)
        if stamp <= 0:
            return False
        limit = self.cfg.chart_stale_secs if which == "spy" else self.cfg.stale_after_secs
        return (time.monotonic() - stamp) <= limit

    def side(self, role: str) -> StreamedContract | None:
        sym = self._roles.get(role)
        return self.options.get(sym) if sym else None

    def es_quote(self) -> dict[str, Any]:
        """Mapped /ES quote (the inner-`quote` shape ESTracker reads), or {} when
        stale so the bot falls back to the REST quote."""
        return _es_stream_to_quote(self._es_raw) if self.is_fresh("es") else {}

    def spy_minute_volume(self) -> float | None:
        return self._spy_minute_vol if self.is_fresh("spy") else None

    def cumdelta_reading(self) -> CumDeltaReading | None:
        """The /ES cumulative-delta exhaustion read, or None when disabled or the
        /ES stream is stale — so the bot/gate/analyst cleanly get no CD signal."""
        if not (CONFIG.cumdelta.enabled and self.is_fresh("es")):
            return None
        return self._cumdelta.reading()

    def status(self) -> dict[str, Any]:
        now = time.monotonic()

        def age(which: str) -> float | None:
            t = self._last.get(which, 0.0)
            return round(now - t, 1) if t > 0 else None

        return {
            "enabled": self.cfg.enabled,
            "connected": self._connected,
            "options": {
                "roles": dict(self._roles),
                "subscribed": sorted(self._opt_active),
                "fresh": self.is_fresh("options"),
                "age_s": age("options"),
            },
            "es": {
                "symbol": self._es_symbol,
                "fresh": self.is_fresh("es"),
                "age_s": age("es"),
                "cum_delta": round(self._cumdelta.cum_delta, 1) if CONFIG.cumdelta.enabled else None,
            },
            "spy": {
                "symbol": self._spy_symbol,
                "fresh": self.is_fresh("spy"),
                "age_s": age("spy"),
                "minute_volume": self._spy_minute_vol,
            },
        }
