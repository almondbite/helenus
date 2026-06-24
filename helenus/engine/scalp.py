"""
EMA Ignition — the 1m 5/9 0DTE contract-scalp confluence.

Replicates a visual scalping strategy: go long when the option contract's 1m
**5 EMA crosses above the 9 EMA** (or reclaims the 5 EMA off a local bottom),
targeting a structural level pre-translated into option premium; the 200 EMA acts
as a dynamic floor/ceiling. The literal edge is that charting the contract
front-runs the index and visualizes "gamma ignition." The weakness is that the
raw 5/9 cross is NOISY — it whipsaws in chop, high-positive-GEX pins, and vanna
(vol-crush) tape.

So the cross is treated as the *trigger, not the edge — the edge is the
filtering*. This module wraps it in a gated confirmation stack and reuses the
structure Helenus already computes:

  Gate 0 — Regime    : advisory, NOT a hard block. Negative/transition GEX (green)
                       is the friendly tape; high-positive GEX (red) still fires but
                       is flagged `slow_grind` (dealer-damped) for the analyst to temper.
  Gate 1 — Vanna     : per-direction premium-vega headwind (falling IV bleeds a
                       long option's premium); see `vanna_headwind`.
  Gate 2 — Confirm   : SPX itself confirms (price on the trade-side of VWAP).
  Gate 3 — De-noise  : a chop counter (N+ 5/9 crosses in a tight window = chop),
                       room-to-target ≥ a minimum, an acceptable contract spread,
                       and a dual-bleed avoidance flag (both ATM wings bleeding).

Design choices (see plan): EMAs run on the clean continuous SPX **index** tape
(MarketState); the selected contract's **premium** is tracked separately for the
gamma-ignition front-run and the dual-bleed read. The decision is a cheap
mechanical PRE-gate (`ScalpReading.active`) that decides whether to spend a
Claude call — Claude still renders the verdict downstream.

Pure where it can be (EMA math, contract selection, premium translation) and
stateful where it must be (`ScalpEngine`, mirroring VannaTracker/ESTracker). The
engine is held by the bot and fed `on_chain` per poll + `on_bar` per bar.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from helenus.config import CONFIG, ScalpConfig
from helenus.engine.charm import CharmProfile
from helenus.engine.flow import VannaReading
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Bar, Direction, KeyLevel, MarketState


# --------------------------------------------------------------------------- #
# Exponential moving averages (recursive — O(1), no history buffer)
# --------------------------------------------------------------------------- #

class _Ema:
    """Single recursive EMA. Seeded with the first sample, then
    `y_t = y_{t-1} + alpha*(x_t - y_{t-1})` with `alpha = 2/(period+1)` —
    identical to pandas `ewm(span=period, adjust=False).mean()`."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.value: float | None = None
        self.n = 0

    def update(self, x: float) -> float:
        if self.value is None:
            self.value = x
        else:
            self.value += self.alpha * (x - self.value)
        self.n += 1
        return self.value

    @property
    def mature(self) -> bool:
        """The EMA is defined from the first sample but only trustworthy once it
        has seen ~period samples — until then it still carries the seed's weight."""
        return self.n >= self.period


class EmaTracker:
    """Fast/slow/trend EMAs over a single timeframe, with cross detection.

    Holds the previous-update fast/slow values so a bar can report whether the
    fast EMA just crossed the slow one (the 5/9 trigger)."""

    def __init__(self, fast: int, slow: int, trend: int) -> None:
        self.fast = _Ema(fast)
        self.slow = _Ema(slow)
        self.trend = _Ema(trend)
        self.prev_fast: float | None = None
        self.prev_slow: float | None = None
        self._has_prev = False

    def update(self, close: float) -> None:
        # Snapshot the pre-update pair so cross_up/cross_dn compares this bar to last.
        self.prev_fast = self.fast.value
        self.prev_slow = self.slow.value
        self._has_prev = self.prev_fast is not None and self.prev_slow is not None
        self.fast.update(close)
        self.slow.update(close)
        self.trend.update(close)

    @property
    def cross_up(self) -> bool:
        return (
            self._has_prev
            and self.prev_fast <= self.prev_slow
            and self.fast.value > self.slow.value
        )

    @property
    def cross_dn(self) -> bool:
        return (
            self._has_prev
            and self.prev_fast >= self.prev_slow
            and self.fast.value < self.slow.value
        )

    @property
    def bull_stack(self) -> bool:
        """Fast above slow — the standing momentum bias (used for the front-run)."""
        return (
            self.fast.value is not None
            and self.slow.value is not None
            and self.fast.value > self.slow.value
        )


# --------------------------------------------------------------------------- #
# Chop counter
# --------------------------------------------------------------------------- #

class ChopTracker:
    """Timestamped 5/9-cross events; `count`/`in_chop` over a trailing window.
    Three-plus crosses inside a few minutes is the chop signature — stand down."""

    def __init__(self) -> None:
        self.events: deque[dt.datetime] = deque(maxlen=64)

    def record(self, ts: dt.datetime) -> None:
        self.events.append(ts)

    def count(self, now: dt.datetime, window_min: int) -> int:
        cutoff = now - dt.timedelta(minutes=window_min)
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        return len(self.events)


# --------------------------------------------------------------------------- #
# Contracts — selection + premium translation
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ContractQuote:
    strike: float
    side: str                    # "call" / "put"
    premium: float               # mark
    bid: float
    ask: float
    delta: float                 # signed (calls +, puts −)
    gamma: float
    volume: float

    @property
    def spread_pct(self) -> float:
        """Bid/ask spread as a fraction of mid (mark fallback). inf if no quote."""
        mid = (self.bid + self.ask) / 2.0 if (self.bid > 0 and self.ask > 0) else self.premium
        if mid <= 0:
            return float("inf")
        return (self.ask - self.bid) / mid


def _f(d: dict[str, Any], key: str) -> float:
    try:
        return float(d.get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def extract_contracts(payload: dict[str, Any]) -> list[ContractQuote]:
    """Raw 0DTE chain JSON -> per-contract quotes (mark/bid/ask/delta/gamma/vol).

    Its own lightweight flatten so gex.CONTRACT_FIELDS stays untouched. Drops
    Schwab's stale-greek sentinels (delta/gamma out of range) so selection and
    premium translation never run on junk."""
    out: list[ContractQuote] = []
    for side, key in (("call", "callExpDateMap"), ("put", "putExpDateMap")):
        for _exp, strikes in (payload.get(key, {}) or {}).items():
            for _sk, contracts in strikes.items():
                for c in contracts:
                    strike = _f(c, "strikePrice")
                    delta = _f(c, "delta")
                    gamma = _f(c, "gamma")
                    mark = _f(c, "mark")
                    if not np.isfinite(strike) or not np.isfinite(mark):
                        continue
                    if not (-1.0 <= delta <= 1.0) or not (0.0 <= gamma <= 1.0):
                        continue  # -999 sentinels / junk greeks
                    out.append(
                        ContractQuote(
                            strike=strike,
                            side=side,
                            premium=max(mark, 0.0),
                            bid=max(_f(c, "bid"), 0.0) if np.isfinite(_f(c, "bid")) else 0.0,
                            ask=max(_f(c, "ask"), 0.0) if np.isfinite(_f(c, "ask")) else 0.0,
                            delta=delta,
                            gamma=gamma,
                            volume=_f(c, "totalVolume") if np.isfinite(_f(c, "totalVolume")) else 0.0,
                        )
                    )
    return out


def select_target_contract(
    contracts: list[ContractQuote],
    spot: float,
    direction: Direction,
    cfg: ScalpConfig | None = None,
) -> ContractQuote | None:
    """The slightly-OTM strike whose |delta| is closest to `target_delta` on the
    correct side — calls above spot for a long, puts below spot for a short."""
    cfg = cfg or CONFIG.scalp
    want = "call" if direction is Direction.BULLISH else "put"
    otm = [
        c
        for c in contracts
        if c.side == want
        and (c.strike > spot if want == "call" else c.strike < spot)
        and abs(abs(c.delta) - cfg.target_delta) <= cfg.delta_band
    ]
    if not otm:
        return None
    return min(otm, key=lambda c: abs(abs(c.delta) - cfg.target_delta))


def premium_target(
    contract: ContractQuote,
    target_price: float,
    spot: float,
    cfg: ScalpConfig | None = None,
) -> float:
    """Translate an index target level into the contract's premium.

    First-order delta translation `prem + delta·Δspot` (delta's sign carries the
    direction: a call's +delta lifts premium as spot rises, a put's −delta lifts
    it as spot falls), plus the second-order `+ ½·gamma·Δspot²` convexity term
    when enabled — the to-the-penny dynamic floor/ceiling the strategy wants.
    Clamped at zero (a contract can't be worth less than nothing)."""
    cfg = cfg or CONFIG.scalp
    d_spot = target_price - spot
    prem = contract.premium + contract.delta * d_spot
    if cfg.use_gamma_in_translation:
        prem += 0.5 * contract.gamma * d_spot * d_spot
    return max(prem, 0.0)


# --------------------------------------------------------------------------- #
# Vanna headwind (per-direction premium-vega read)
# --------------------------------------------------------------------------- #

def vanna_headwind(
    direction: Direction, vix_change: float, cfg: ScalpConfig | None = None
) -> bool:
    """Does the vol regime fight this direction's *premium*?

    Falling IV bleeds vega out of any long 0DTE option, so a strongly falling VIX
    is a headwind for both a long call (the classic post-open vanna crush that
    makes calls a net drag) and a long put (a calm melt-up crushes put premium and
    rarely sells off). A rising-IV tape is the vol-expansion regime where puts run
    and calls get a vega tailwind — no headwind either way. This is a distinct
    axis from the vanna *rally* (a spot-direction signal); here we read premium
    quality. `direction` is carried for the note and any future asymmetry."""
    cfg = cfg or CONFIG.scalp
    _ = direction
    return vix_change <= -cfg.vix_headwind_pts


# --------------------------------------------------------------------------- #
# Premium-series reads (dual-bleed avoidance + divergence booster)
# --------------------------------------------------------------------------- #

def _bleeding(vals: list[float]) -> bool:
    """A premium series making lower highs AND lower lows over the window — the
    organic bleed of a decaying contract (split halves, compare extremes)."""
    if len(vals) < 4:
        return False
    mid = len(vals) // 2
    early, late = vals[:mid], vals[mid:]
    return max(late) < max(early) and min(late) < min(early)


def _higher_low(vals: list[float]) -> bool:
    """Recent half's trough is ABOVE the earlier half's trough — the contract
    refusing to make a new low while the underlying does (vega being bid)."""
    if len(vals) < 4:
        return False
    mid = len(vals) // 2
    return min(vals[mid:]) > min(vals[:mid])


def _lower_low(vals: list[float]) -> bool:
    if len(vals) < 4:
        return False
    mid = len(vals) // 2
    return min(vals[mid:]) < min(vals[:mid])


# --------------------------------------------------------------------------- #
# The per-bar reading
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ScalpReading:
    """One bar's EMA-ignition read: the trigger, the gates, and the trade plan."""
    # Trigger
    direction: Direction | None = None
    cross_type: str = "none"            # "cross" | "reclaim" | "none"
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_trend: float | None = None
    front_run: bool = False             # contract premium already crossed in-dir
    # De-noise / avoidance
    chop_count: int = 0
    in_chop: bool = False
    dual_bleed: bool = False
    premium_divergence: bool = False
    vanna_headwind: bool = False
    # Trade plan
    target_contract: ContractQuote | None = None
    target_level: KeyLevel | None = None
    premium_target: float | None = None
    room_to_level_pts: float = float("inf")
    spread_ok: bool = True
    # Gate booleans
    regime_ok: bool = False
    slow_grind: bool = False            # fired in high-positive GEX (dealer-damped)
    vanna_ok: bool = False
    confirm_ok: bool = False
    room_ok: bool = False
    chop_ok: bool = True
    bleed_ok: bool = True
    # Verdict
    active: bool = False
    note: str = ""

    @property
    def ema_stack(self) -> str:
        if self.ema_fast is None or self.ema_slow is None:
            return "n/a"
        return "5>9" if self.ema_fast > self.ema_slow else "5<9"


# --------------------------------------------------------------------------- #
# The stateful engine (held by the bot)
# --------------------------------------------------------------------------- #

class ScalpEngine:
    """Owns the EMA/chop/premium state. `on_chain` feeds the contract table and
    ATM-wing premium series each poll; `on_bar` updates the index EMAs from the
    latest closed bar and returns a `ScalpReading`."""

    def __init__(self, cfg: ScalpConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.scalp
        self.ema = EmaTracker(self.cfg.ema_fast, self.cfg.ema_slow, self.cfg.ema_trend)
        self.chop = ChopTracker()
        # ATM-wing premium series for the dual-bleed read, plus their own 5/9 EMAs
        # for the gamma-ignition front-run (premium cross leads the index cross).
        self._atm_call: deque[float] = deque(maxlen=self.cfg.premium_history)
        self._atm_put: deque[float] = deque(maxlen=self.cfg.premium_history)
        self._call_ema = EmaTracker(self.cfg.ema_fast, self.cfg.ema_slow, self.cfg.ema_trend)
        self._put_ema = EmaTracker(self.cfg.ema_fast, self.cfg.ema_slow, self.cfg.ema_trend)
        self._contracts: list[ContractQuote] = []
        self._last_bar_ts: dt.datetime | None = None

    # -- warmup ------------------------------------------------------------ #

    def warm(self, bars: list[Bar]) -> None:
        """Seed the index EMAs from the backfilled tape so a mid-session restart
        doesn't start with cold EMAs (no false cross on the first live bar).
        Records the last seen bar timestamp so `on_bar` won't reprocess it."""
        for b in bars:
            self.ema.update(b.close)
        if bars:
            self._last_bar_ts = bars[-1].ts

    # -- per chain poll ---------------------------------------------------- #

    def on_chain(self, payload: dict[str, Any], spot: float) -> None:
        """Cache the contract table and push the ATM call/put marks (for the
        dual-bleed read + the premium front-run EMAs). Cheap; runs every poll."""
        self._contracts = extract_contracts(payload)
        if spot <= 0 or not self._contracts:
            return
        calls = [c for c in self._contracts if c.side == "call"]
        puts = [c for c in self._contracts if c.side == "put"]
        atm_call = min(calls, key=lambda c: abs(c.strike - spot), default=None)
        atm_put = min(puts, key=lambda c: abs(c.strike - spot), default=None)
        if atm_call is not None and atm_call.premium > 0:
            self._atm_call.append(atm_call.premium)
            self._call_ema.update(atm_call.premium)
        if atm_put is not None and atm_put.premium > 0:
            self._atm_put.append(atm_put.premium)
            self._put_ema.update(atm_put.premium)

    # -- per bar ----------------------------------------------------------- #

    def on_bar(
        self,
        state: MarketState,
        gex: GexProfile | None,
        vanna: VannaReading | None,
        charm: CharmProfile | None,
    ) -> ScalpReading:
        """Update the index EMAs with the latest closed bar and evaluate the
        trigger + gate stack into a `ScalpReading`."""
        if not state.bars:
            return ScalpReading()
        cur = state.bars[-1]
        # Dedupe: warm() / a prior on_bar may already have consumed this bar.
        if self._last_bar_ts is not None and cur.ts == self._last_bar_ts:
            return self._inactive()
        self._last_bar_ts = cur.ts

        self.ema.update(cur.close)
        if self.ema.cross_up or self.ema.cross_dn:
            self.chop.record(cur.ts)

        reclaim_up, reclaim_dn = self._reclaim(state)

        if self.ema.cross_up or reclaim_up:
            direction: Direction | None = Direction.BULLISH
        elif self.ema.cross_dn or reclaim_dn:
            direction = Direction.BEARISH
        else:
            direction = None

        cross_type = (
            "cross" if (self.ema.cross_up or self.ema.cross_dn)
            else "reclaim" if (reclaim_up or reclaim_dn)
            else "none"
        )

        ema_fast, ema_slow, ema_trend = self.ema.fast.value, self.ema.slow.value, self.ema.trend.value

        if direction is None:
            # No fresh trigger this bar — still surface the EMA state for the board.
            return ScalpReading(
                cross_type="none",
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                ema_trend=ema_trend,
                chop_count=self.chop.count(cur.ts, self.cfg.chop_window_min),
                note="no fresh 5/9 cross or reclaim",
            )

        spot = cur.close
        vix_change = vanna.vix_change if vanna is not None else 0.0

        # --- gates --------------------------------------------------------- #
        regime_ok, regime_tag = self._regime_ok(gex)
        headwind = vanna_headwind(direction, vix_change, self.cfg)
        vanna_ok = not headwind
        confirm_ok = self._confirm_ok(state, direction, spot)

        target_level, room = self._target_level(state, gex, charm, direction, spot, ema_trend)
        room_ok = room >= self.cfg.min_room_pts

        chop_count = self.chop.count(cur.ts, self.cfg.chop_window_min)
        in_chop = chop_count >= self.cfg.chop_max_crosses
        chop_ok = not in_chop

        dual_bleed = _bleeding(list(self._atm_call)) and _bleeding(list(self._atm_put))
        bleed_ok = not dual_bleed

        contract = select_target_contract(self._contracts, spot, direction, self.cfg)
        spread_ok = contract is not None and contract.spread_pct <= self.cfg.max_spread_pct
        prem_target = (
            premium_target(contract, target_level.price, spot, self.cfg)
            if (contract is not None and target_level is not None)
            else None
        )

        front_run = (
            self._call_ema.bull_stack if direction is Direction.BULLISH
            else self._put_ema.bull_stack
        )
        premium_div = self._premium_divergence(state, direction)

        # Gate 0 (regime) is no longer a HARD block: momentum may fire in high-
        # positive GEX too — it's just flagged as a dealer-damped "slow grind" for
        # the analyst to temper, not auto-rejected.
        slow_grind = regime_tag == "red"
        active = (
            vanna_ok and confirm_ok and room_ok
            and chop_ok and bleed_ok and spread_ok and contract is not None
        )

        note = self._note(
            direction, cross_type, regime_tag, room, chop_count, dual_bleed,
            headwind, front_run, premium_div, contract, target_level, prem_target,
            slow_grind, active,
        )

        return ScalpReading(
            direction=direction,
            cross_type=cross_type,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema_trend=ema_trend,
            front_run=front_run,
            chop_count=chop_count,
            in_chop=in_chop,
            dual_bleed=dual_bleed,
            premium_divergence=premium_div,
            vanna_headwind=headwind,
            target_contract=contract,
            target_level=target_level,
            premium_target=prem_target,
            room_to_level_pts=room,
            spread_ok=spread_ok,
            regime_ok=regime_ok,
            slow_grind=slow_grind,
            vanna_ok=vanna_ok,
            confirm_ok=confirm_ok,
            room_ok=room_ok,
            chop_ok=chop_ok,
            bleed_ok=bleed_ok,
            active=active,
            note=note,
        )

    # -- internals --------------------------------------------------------- #

    def _inactive(self) -> ScalpReading:
        return ScalpReading(
            ema_fast=self.ema.fast.value,
            ema_slow=self.ema.slow.value,
            ema_trend=self.ema.trend.value,
        )

    def _reclaim(self, state: MarketState) -> tuple[bool, bool]:
        """Reclaim of the 5 EMA off a local bottom/top (higher quality than a raw
        cross — it has a defined invalidation). Bullish: the prior bar closed
        below the 5 EMA, this bar closes back above it, and price has bounced off a
        recent local low. Mirror for bearish. Needs the prior fast EMA value the
        tracker just snapshotted."""
        if self.ema.prev_fast is None or self.ema.fast.value is None or len(state.bars) < 3:
            return False, False
        cur = state.bars[-1]
        prev = state.bars[-2]
        k = self.cfg.reclaim_lookback_bars
        window = [b.close for b in list(state.bars)[-(k + 1):-1]]  # exclude current
        if not window:
            return False, False
        reclaim_up = (
            prev.close < self.ema.prev_fast
            and cur.close >= self.ema.fast.value
            and min(window) < cur.close            # bounced off a local low
            and not self.ema.cross_up              # distinct from the raw 5/9 cross
        )
        reclaim_dn = (
            prev.close > self.ema.prev_fast
            and cur.close <= self.ema.fast.value
            and max(window) > cur.close            # rejected off a local high
            and not self.ema.cross_dn
        )
        return reclaim_up, reclaim_dn

    def _regime_ok(self, gex: GexProfile | None) -> tuple[bool, str]:
        """Gate 0 (advisory): momentum is friendly in negative/transition GEX
        (green) and dealer-damped in high-positive GEX (red). No longer a hard
        block — `red` just sets the `slow_grind` flag; a conflicted positive label
        is treated as transition (amber)."""
        if gex is None:
            return False, "no-gex"
        regime = gex.regime
        if "NEGATIVE" in regime:
            return True, "green"
        if "CONFLICTED" in regime:
            return True, "transition"
        if regime == "POSITIVE_GAMMA":
            return False, "red"
        return False, regime.lower()

    def _confirm_ok(self, state: MarketState, direction: Direction, spot: float) -> bool:
        """Gate 2: SPX itself confirms. Prefer price on the trade-side of VWAP (the
        day's gravity line); fall back to the structural trend when VWAP isn't warm
        yet, and to True only when neither read exists (very early session)."""
        vwap = state.vwap()
        if vwap == vwap:  # not NaN
            return spot >= vwap if direction is Direction.BULLISH else spot <= vwap
        trend = state.trend_direction()
        if trend is not None:
            return trend == direction
        return True

    def _target_level(
        self,
        state: MarketState,
        gex: GexProfile | None,
        charm: CharmProfile | None,
        direction: Direction,
        spot: float,
        ema_trend: float | None,
    ) -> tuple[KeyLevel | None, float]:
        """Nearest structural level in the trade direction — the strategy's
        dynamic ceiling (long) / floor (short). Drawn from the key-level grid
        (GEX/charm walls, VWAP, session extremes, round numbers) plus the 200 EMA
        as a dynamic level when it's on the right side. Returns (level, room_pts);
        room is +inf when nothing sits ahead (open air)."""
        levels = list(state.key_levels(spot, gex, charm))
        if ema_trend is not None and self.ema.trend.mature:
            levels.append(KeyLevel(float(ema_trend), "200 EMA", weight=0.7))
        if direction is Direction.BULLISH:
            ahead = [lv for lv in levels if lv.price > spot]
            nearest = min(ahead, key=lambda lv: lv.price - spot, default=None)
            room = (nearest.price - spot) if nearest else float("inf")
        else:
            ahead = [lv for lv in levels if lv.price < spot]
            nearest = min(ahead, key=lambda lv: spot - lv.price, default=None)
            room = (spot - nearest.price) if nearest else float("inf")
        return nearest, room

    def _premium_divergence(self, state: MarketState, direction: Direction) -> bool:
        """Booster: the contract refuses to confirm the index's extreme. Bullish —
        the index prints a lower low while the ATM call premium prints a higher low
        (vega being bid into the reversal). Bearish mirror on the put."""
        k = self.cfg.dual_bleed_lookback
        idx = [b.close for b in list(state.bars)[-(2 * k):]]
        if direction is Direction.BULLISH:
            return _lower_low(idx) and _higher_low(list(self._atm_call))
        return _higher_high(idx) and _higher_low(list(self._atm_put))

    def _note(
        self, direction, cross_type, regime_tag, room, chop_count, dual_bleed,
        headwind, front_run, premium_div, contract, target_level, prem_target,
        slow_grind, active,
    ) -> str:
        d = direction.value
        trig = "5/9 cross" if cross_type == "cross" else "5 EMA reclaim"
        parts = [f"{d} {trig}", f"regime {regime_tag}"]
        if target_level is not None:
            parts.append(f"target {target_level.label} @ {target_level.price:.0f} ({room:.1f}pt)")
        if contract is not None:
            strike = f"{contract.strike:.0f}{'C' if contract.side == 'call' else 'P'}"
            tgt = f" → {prem_target:.2f}" if prem_target is not None else ""
            parts.append(f"{strike} @ {contract.premium:.2f}{tgt}")
        flags = []
        if slow_grind:
            flags.append("slow grind (high+GEX)")
        if headwind:
            flags.append("vanna headwind")
        if dual_bleed:
            flags.append("dual-bleed")
        if chop_count:
            flags.append(f"{chop_count} crosses/{self.cfg.chop_window_min}m")
        if front_run:
            flags.append("premium front-run")
        if premium_div:
            flags.append("premium divergence")
        if flags:
            parts.append("; ".join(flags))
        parts.append("ACTIVE" if active else "gated")
        return " | ".join(parts)


def _higher_high(vals: list[float]) -> bool:
    if len(vals) < 4:
        return False
    mid = len(vals) // 2
    return max(vals[mid:]) > max(vals[:mid])
