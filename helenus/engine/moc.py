"""
MOC — the Market-On-Close power-hour play (~3:50–4:00 ET).

The 4:00 ET closing auction forces enormous passive flow (index funds, ETF
rebalances, fund creations/redemptions) to execute at the print, so the tape
gets magnetized into the close and tends to **reverse then drift** in recurring
patterns. Those patterns DRIFT over time, so the engine surfaces them as priors
and logs them — the journal→review→lessons loop learns which still pay.

No imbalance feed is used (it isn't in the Schwab API and, per the design, isn't
needed). The play is read from mechanical proxies, deliberately using FEW
variables — only options premium and options volume for the reversal:

  1. REVERSAL (priority, 3:50–3:55) — a simplified premium-behavior read: one side
     (call/put) premium BASES (prints a higher-low off a decline) while that side's
     fresh interval volume SURGES vs the opposing side → reversal in that side's
     favor (put side → bearish, call side → bullish). This is the 7375P tell: the
     put based + put volume outran calls after 3:50, and SPX reversed down into 4:00.
  2. CAPITULATION candle — a contract's 1m PREMIUM candle wicks far above its own
     5/9/200 EMAs (the "instant buy & sell book" mechanical order) then rejects with
     a red wick; premium undercuts much lower, then reverses back past the wick. The
     clean entry is the RECLAIM after the undercut, not the wick. Fires intraday too.
  3. The inverse 5m candle-color heuristic — the 5-min index candle into 3:50:
     green → dump into MOC (bearish), red → pump/uppercut (bullish). A drifting prior.
  4. GEX context — "buy into overshoot, sell into pin": is spot pinned at a wall /
     zero-Γ, or overshooting beyond the gamma envelope.

Stateful (mirrors ScalpEngine): `on_chain` caches the ATM call/put premium + volume
each poll and folds 1m premium candles; `on_bar` closes those candles, runs the
capitulation / reversal / heuristic / gex reads, and returns a `MocReading`. Per-
contract premium OHLC is approximated from ~15s chain polls — the same intra-bar
sampling the index tape uses (true OHLC would need the schwab-py StreamClient).
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from helenus.config import CONFIG, MocConfig
from helenus.engine.gex import GexProfile
from helenus.engine.scalp import (
    ContractQuote,
    EmaTracker,
    _higher_low,
    extract_contracts,
)
from helenus.engine.scan2 import Bar, Direction, MarketState


# --------------------------------------------------------------------------- #
# Per-side premium state (call / put)
# --------------------------------------------------------------------------- #

class _PremiumSide:
    """One side's (call or put) ATM-contract premium state: the premium sample
    series for the basing read, the 1m premium-candle EMAs for the capitulation
    read, the forming candle's extremes, and the fresh interval volume."""

    def __init__(self, cfg: MocConfig) -> None:
        self.cfg = cfg
        self.prem: deque[float] = deque(maxlen=cfg.premium_history)
        self.ema = EmaTracker(cfg.ema_fast, cfg.ema_slow, cfg.ema_trend)
        # Forming 1m premium candle, folded from chain-poll marks between bars.
        self._open: float | None = None
        self._high: float = float("-inf")
        self._low: float = float("inf")
        self._close: float | None = None
        # Last finalized candle: (open, high, low, close).
        self.last_candle: tuple[float, float, float, float] | None = None
        # Fresh contract volume this bar (interval delta, guarded on strike change).
        self.interval_vol: float = 0.0
        self._last_cum_vol: float | None = None
        self._last_strike: float | None = None

    def reset(self) -> None:
        self.prem.clear()
        self.ema = EmaTracker(self.cfg.ema_fast, self.cfg.ema_slow, self.cfg.ema_trend)
        self._open = self._close = None
        self._high, self._low = float("-inf"), float("inf")
        self.last_candle = None
        self.interval_vol = 0.0
        self._last_cum_vol = None
        self._last_strike = None

    def observe(self, c: ContractQuote) -> None:
        """Fold one chain-poll quote into the forming candle + the volume delta."""
        prem = c.premium
        if prem <= 0:
            return
        self.prem.append(prem)
        if self._open is None:
            self._open = prem
        self._high = max(self._high, prem)
        self._low = min(self._low, prem)
        self._close = prem
        # Interval volume: the contract's cumulative totalVolume delta. The ATM
        # strike drifts as spot moves, so a strike change resets the baseline
        # (the new contract's cumulative is unrelated) instead of printing a
        # spurious jump.
        if self._last_strike is not None and c.strike == self._last_strike and self._last_cum_vol is not None:
            self.interval_vol += max(0.0, c.volume - self._last_cum_vol)
        self._last_strike = c.strike
        self._last_cum_vol = c.volume

    def load_stream(
        self,
        candle: tuple[float, float, float, float] | None,
        marks: list[float],
        interval_vol: float,
    ) -> None:
        """Populate this bar's premium state from the real-time stream instead of
        poll-folded marks — the same fields `observe` fills, so `finalize_candle` /
        the reversal read stay agnostic to the source. The streamed candle carries
        true intra-bar wicks (the whole point of streaming for capitulation)."""
        if candle is not None:
            self._open, self._high, self._low, self._close = candle
        for m in marks:
            if m > 0:
                self.prem.append(m)
        self.interval_vol = interval_vol

    def finalize_candle(self) -> None:
        """Close the forming premium candle (does NOT advance the EMA — that
        happens after the capitulation check so the wick is measured against the
        EMA it actually pierced)."""
        if self._open is not None and self._close is not None:
            self.last_candle = (self._open, self._high, self._low, self._close)
        else:
            self.last_candle = None
        self._open = self._close = None
        self._high, self._low = float("-inf"), float("inf")

    def advance_ema(self) -> None:
        if self.last_candle is not None:
            self.ema.update(self.last_candle[3])

    def mature_emas(self) -> list[float]:
        return [
            e.value
            for e in (self.ema.fast, self.ema.slow, self.ema.trend)
            if e.mature and e.value is not None
        ]


# --------------------------------------------------------------------------- #
# The per-bar reading
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MocReading:
    """One bar's MOC read: the window phase, the close-play priors (heuristic +
    GEX context), the simplified premium-behavior reversal, and the capitulation
    candle. `reversal_active` / `capitulation` are the mechanical gate triggers."""
    phase: str = "pre"                  # pre | brief | reversal | late | closed
    minutes_to_close: float | None = None
    in_window: bool = False             # inside the MOC window (3:50–4:00)
    in_reversal_window: bool = False    # inside the reversal window (3:50–3:55)
    # 5m candle-color heuristic (inverse): green → bearish, red → bullish.
    heuristic_color: str | None = None  # green | red | doji
    heuristic_bias: Direction | None = None
    # GEX context for "buy overshoot, sell pin".
    gex_state: str = "NEUTRAL"          # PIN | OVERSHOOT | NEUTRAL
    nearest_wall: float | None = None
    # Premium-behavior reversal.
    call_premium: float | None = None
    put_premium: float | None = None
    call_volume: float = 0.0
    put_volume: float = 0.0
    basing_side: str | None = None      # "call" | "put"
    volume_surge_ratio: float = 0.0
    reversal_direction: Direction | None = None
    reversal_active: bool = False
    # Capitulation candle.
    capitulation: bool = False
    cap_side: str | None = None         # "call" | "put"
    cap_high: float | None = None
    cap_close: float | None = None
    cap_wick_frac: float | None = None
    note: str = ""


# --------------------------------------------------------------------------- #
# The stateful engine (held by the bot)
# --------------------------------------------------------------------------- #

class MocEngine:
    """Owns the per-side premium state. `on_chain` caches ATM call/put premium +
    volume each poll; `on_bar` closes the premium candles and evaluates the
    close-play reads into a `MocReading`. Resets daily."""

    def __init__(self, cfg: MocConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.moc
        self.call = _PremiumSide(self.cfg)
        self.put = _PremiumSide(self.cfg)
        self._date: dt.date | None = None

    # -- timing ------------------------------------------------------------ #

    def _at(self, ts: dt.datetime, hour: int, minute: int) -> dt.datetime:
        return ts.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _phase(self, ts: dt.datetime) -> str:
        brief = self._at(ts, self.cfg.brief_hour, self.cfg.brief_minute)
        open_ = self._at(ts, self.cfg.window_open_hour, self.cfg.window_open_minute)
        rev_end = self._at(ts, self.cfg.reversal_deadline_hour, self.cfg.reversal_deadline_minute)
        close = self._at(ts, self.cfg.close_hour, self.cfg.close_minute)
        if ts < brief:
            return "pre"
        if ts < open_:
            return "brief"
        if ts < rev_end:
            return "reversal"
        if ts < close:
            return "late"
        return "closed"

    def _reset(self, day: dt.date) -> None:
        self._date = day
        self.call.reset()
        self.put.reset()

    # -- per chain poll ---------------------------------------------------- #

    def on_chain(self, payload: dict[str, Any], spot: float) -> None:
        """Cache the ATM call + ATM put premium / volume (fold a 1m premium candle
        + accumulate the interval volume delta). Cheap; runs every poll."""
        if spot <= 0:
            return
        contracts = extract_contracts(payload)
        if not contracts:
            return
        calls = [c for c in contracts if c.side == "call"]
        puts = [c for c in contracts if c.side == "put"]
        atm_call = min(calls, key=lambda c: abs(c.strike - spot), default=None)
        atm_put = min(puts, key=lambda c: abs(c.strike - spot), default=None)
        if atm_call is not None:
            self.call.observe(atm_call)
        if atm_put is not None:
            self.put.observe(atm_put)

    # -- per bar: streamed source (preferred when the websocket is live) --- #

    def feed_stream(
        self,
        call_payload: tuple | None,
        put_payload: tuple | None,
    ) -> None:
        """Load this bar's ATM call/put premium from the real-time stream (each
        payload is `StreamedContract.roll()` → (candle, marks, interval_vol), or
        None). Called by the bot in place of `on_chain` when the stream is fresh,
        so the capitulation wick + reversal read off true per-contract ticks. The
        following `on_bar` then finalizes exactly as on the poll path."""
        if call_payload is not None:
            self.call.load_stream(*call_payload)
        if put_payload is not None:
            self.put.load_stream(*put_payload)

    # -- per bar ----------------------------------------------------------- #

    def on_bar(
        self, state: MarketState, gex: GexProfile | None, now: dt.datetime
    ) -> MocReading:
        """Close the premium candles, evaluate the close-play reads, and return a
        `MocReading`. Called every regular-session bar (capitulation is intraday)."""
        day = now.date()
        if day != self._date:
            self._reset(day)

        # 1. Close the forming premium candles (EMA advance deferred to step 3).
        self.call.finalize_candle()
        self.put.finalize_candle()

        # 2. Snapshot this bar's per-side interval volume, then reset for next bar.
        call_vol, put_vol = self.call.interval_vol, self.put.interval_vol
        self.call.interval_vol = self.put.interval_vol = 0.0

        # 3. Capitulation reads the wick against the EMA it pierced (prior EMA),
        #    THEN the EMAs advance with this candle's close.
        cap_side, cap_stats = self._capitulation()
        self.call.advance_ema()
        self.put.advance_ema()

        phase = self._phase(now)
        close_dt = self._at(now, self.cfg.close_hour, self.cfg.close_minute)
        minutes_to_close = round((close_dt - now).total_seconds() / 60.0, 1)
        in_window = phase in ("reversal", "late")
        in_reversal = phase == "reversal"

        heuristic_color, heuristic_bias = (
            self._heuristic(state) if phase in ("brief", "reversal", "late") else (None, None)
        )
        gex_state, nearest_wall = self._gex_state(gex)
        basing_side, surge, rev_dir = self._reversal(call_vol, put_vol)
        reversal_active = rev_dir is not None and in_reversal

        call_prem = self.call.prem[-1] if self.call.prem else None
        put_prem = self.put.prem[-1] if self.put.prem else None
        capitulation = cap_side is not None
        cap_high = cap_stats[0] if cap_stats else None
        cap_close = cap_stats[1] if cap_stats else None
        cap_wick = cap_stats[2] if cap_stats else None

        note = self._note(
            phase, minutes_to_close, heuristic_color, heuristic_bias, gex_state,
            nearest_wall, basing_side, surge, rev_dir, reversal_active,
            cap_side, cap_high, cap_close,
        )
        return MocReading(
            phase=phase,
            minutes_to_close=minutes_to_close,
            in_window=in_window,
            in_reversal_window=in_reversal,
            heuristic_color=heuristic_color,
            heuristic_bias=heuristic_bias,
            gex_state=gex_state,
            nearest_wall=nearest_wall,
            call_premium=round(call_prem, 2) if call_prem is not None else None,
            put_premium=round(put_prem, 2) if put_prem is not None else None,
            call_volume=round(call_vol),
            put_volume=round(put_vol),
            basing_side=basing_side,
            volume_surge_ratio=round(surge, 2),
            reversal_direction=rev_dir,
            reversal_active=reversal_active,
            capitulation=capitulation,
            cap_side=cap_side,
            cap_high=round(cap_high, 2) if cap_high is not None else None,
            cap_close=round(cap_close, 2) if cap_close is not None else None,
            cap_wick_frac=cap_wick,
            note=note,
        )

    # -- internals --------------------------------------------------------- #

    def _heuristic(self, state: MarketState) -> tuple[str | None, Direction | None]:
        """The N-min index candle ending at window_open, mapped INVERSELY: a green
        candle into 3:50 tends to dump into the MOC (bearish), a red one tends to
        pump/uppercut (bullish). A drifting prior, not a hard signal."""
        if not state.bars:
            return None, None
        last = state.bars[-1].ts
        open_ = self._at(last, self.cfg.window_open_hour, self.cfg.window_open_minute)
        start = open_ - dt.timedelta(minutes=self.cfg.heuristic_candle_min)
        window = [b for b in state.bars if start < b.ts <= open_]
        if not window:
            return None, None
        o, c = window[0].open, window[-1].close
        if c > o:
            return "green", Direction.BEARISH
        if c < o:
            return "red", Direction.BULLISH
        return "doji", None

    def _gex_state(self, gex: GexProfile | None) -> tuple[str, float | None]:
        """PIN = spot hugging a wall/zero-Γ; OVERSHOOT = spot beyond the gamma
        envelope (above the top call wall or below the bottom put wall); else
        NEUTRAL. Feeds the 'buy overshoot, sell pin' prior."""
        if gex is None:
            return "NEUTRAL", None
        spot = gex.spot
        call_ks = [k for k, _ in gex.call_walls]
        put_ks = [k for k, _ in gex.put_walls]
        walls = call_ks + put_ks + ([gex.zero_gamma] if gex.zero_gamma is not None else [])
        if not walls:
            return "NEUTRAL", None
        nearest = min(walls, key=lambda k: abs(k - spot))
        overshoot = (call_ks and spot > max(call_ks)) or (put_ks and spot < min(put_ks))
        if overshoot:
            state = "OVERSHOOT"
        elif abs(nearest - spot) <= self.cfg.pin_proximity_pts:
            state = "PIN"
        else:
            state = "NEUTRAL"
        return state, round(float(nearest), 0)

    def _reversal(
        self, call_vol: float, put_vol: float
    ) -> tuple[str | None, float, Direction | None]:
        """The simplified premium-behavior reversal — ONLY premium + volume. The
        side whose premium bases (higher-low off a decline) AND whose fresh volume
        outruns the opposing side by `volume_surge_ratio` is the reversal side
        (put → bearish, call → bullish)."""
        cfg = self.cfg
        put_base = _higher_low(list(self.put.prem))
        call_base = _higher_low(list(self.call.prem))
        if (
            put_base and put_vol >= cfg.min_side_volume
            and put_vol >= cfg.volume_surge_ratio * max(call_vol, 1.0)
        ):
            return "put", put_vol / max(call_vol, 1.0), Direction.BEARISH
        if (
            call_base and call_vol >= cfg.min_side_volume
            and call_vol >= cfg.volume_surge_ratio * max(put_vol, 1.0)
        ):
            return "call", call_vol / max(put_vol, 1.0), Direction.BULLISH
        return None, 0.0, None

    def _capitulation(
        self,
    ) -> tuple[str | None, tuple[float, float, float] | None]:
        """A side's just-closed premium candle wicked above ALL its mature EMAs
        then rejected (a red wick). Returns the side + (high, close, wick_frac).
        Not window-gated — capitulation happens intraday too."""
        cfg = self.cfg
        for label, side in (("call", self.call), ("put", self.put)):
            candle = side.last_candle
            if candle is None:
                continue
            _o, high, low, close = candle
            rng = high - low
            if rng <= 0:
                continue
            wick_frac = (high - close) / rng
            emas = side.mature_emas()
            if not emas:
                continue
            if high > max(emas) * (1.0 + cfg.cap_ema_margin) and wick_frac >= cfg.cap_wick_frac:
                return label, (high, close, round(wick_frac, 2))
        return None, None

    def _note(
        self, phase, minutes_to_close, heuristic_color, heuristic_bias, gex_state,
        nearest_wall, basing_side, surge, rev_dir, reversal_active,
        cap_side, cap_high, cap_close,
    ) -> str:
        if phase in ("pre", "closed"):
            return f"MOC {phase} ({minutes_to_close:+.0f}m to close)"
        parts = [f"MOC {phase} ({minutes_to_close:+.0f}m)"]
        if heuristic_bias is not None:
            parts.append(f"5m {heuristic_color}→{heuristic_bias.value}")
        parts.append(f"GEX {gex_state}" + (f" @ {nearest_wall:.0f}" if nearest_wall else ""))
        if cap_side is not None:
            parts.append(f"CAPITULATION {cap_side} wick {cap_high:.2f}→{cap_close:.2f}")
        if reversal_active and rev_dir is not None:
            parts.append(
                f"REVERSAL {rev_dir.value}: {basing_side} based + {surge:.1f}× volume"
            )
        elif basing_side is not None:
            parts.append(f"{basing_side} basing ({surge:.1f}× vol)")
        return " | ".join(parts)
