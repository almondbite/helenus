"""
Opening Range Breakout (ORB) — the first-N-minute range as the session's pivot.

When the bell rings, a flood of liquidity (overnight news, repositioning) carves
out a defined price range. The ORB premise: whichever way price *closes* out of
that opening range tends to set the dominant direction for the session.

Mechanics:
  1. Lock the absolute high/low of the first `window_min` minutes after the 09:30
     ET open. After the window, the range is fixed for the day.
  2. A 1-minute bar CLOSING above the range high is a long; closing below the low
     is a short (a close, not just a wick — the standard fakeout guard).
  3. Targets are R-multiples of the range height (1R, 2R, ...); the stop is the
     opposite edge.

The single biggest risk is the false breakout, so two hard filters gate it (see
plan): volume confirmation (the breakout bar's volume clears a baseline multiple
— institutional participation) and VWAP alignment (a long is skipped while price
is below the day's VWAP, a short while above). Thresholds live in CONFIG.orb.

Session-stateful (the locked range outlives the bars that formed it once they
roll off MarketState's bounded deque), so this is an engine the bot holds and
feeds per bar — mirroring ScalpEngine. Pure decision logic, no I/O.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from helenus.config import CONFIG, ORBConfig
from helenus.engine.scan2 import Bar, Direction


@dataclass(frozen=True)
class ORBReading:
    """The opening-range state for one bar: the locked range, whether this bar
    broke it, the fakeout filters, and the R-multiple trade plan."""
    range_high: float | None = None
    range_low: float | None = None
    range_pts: float | None = None
    locked: bool = False
    in_window: bool = False             # still inside the opening-range window
    direction: Direction | None = None  # the breakout this bar produced (if any)
    volume_ok: bool = False
    vwap_ok: bool = False
    entry: float | None = None          # the range edge that broke
    targets: list[float] = field(default_factory=list)
    stop: float | None = None
    active: bool = False                # a confirmed, filtered breakout this bar
    note: str = ""


class ORBEngine:
    """Tracks the opening range and detects filtered breakout closes. Resets
    daily; fires at most once per side per session."""

    def __init__(self, cfg: ORBConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.orb
        self._date: dt.date | None = None
        self._hi: float = -np.inf
        self._lo: float = np.inf
        self._locked: bool = False
        self._fired_up: bool = False
        self._fired_dn: bool = False

    # ------------------------------------------------------------------ #

    def _reset(self, day: dt.date) -> None:
        self._date = day
        self._hi, self._lo = -np.inf, np.inf
        self._locked = self._fired_up = self._fired_dn = False

    def _open(self, ts: dt.datetime) -> dt.datetime:
        return ts.replace(
            hour=self.cfg.open_hour, minute=self.cfg.open_minute,
            second=0, microsecond=0,
        )

    def _phase(self, ts: dt.datetime) -> str:
        """'pre' (before the open), 'window' (building the range), or 'open'
        (range locked)."""
        open_ts = self._open(ts)
        if ts < open_ts:
            return "pre"
        if ts < open_ts + dt.timedelta(minutes=self.cfg.window_min):
            return "window"
        return "open"

    def _plan(self, direction: Direction):
        """Entry (the broken edge), R-multiple targets, and the stop (far edge)."""
        rng = self._hi - self._lo
        if direction is Direction.BULLISH:
            entry, stop = self._hi, self._lo
            targets = [round(entry + r * rng, 2) for r in self.cfg.r_multiples]
        else:
            entry, stop = self._lo, self._hi
            targets = [round(entry - r * rng, 2) for r in self.cfg.r_multiples]
        return entry, targets, stop, rng

    # ------------------------------------------------------------------ #

    def warm(self, bars: list[Bar]) -> None:
        """Replay backfilled bars to rebuild the locked range and pre-set the
        per-side fired flags from historical closes — so a mid-session restart
        re-establishes the range without re-alerting a breakout that already
        happened. Produces no readings."""
        for b in bars:
            day = b.ts.date()
            if day != self._date:
                self._reset(day)
            phase = self._phase(b.ts)
            if phase == "window":
                self._hi = max(self._hi, b.high)
                self._lo = min(self._lo, b.low)
            elif phase == "open":
                self._locked = True
                if np.isfinite(self._hi):
                    if b.close > self._hi:
                        self._fired_up = True
                    elif b.close < self._lo:
                        self._fired_dn = True

    def on_bar(self, bar: Bar, vwap: float, volume_ratio: float) -> ORBReading:
        """Update the range / detect a filtered breakout for the just-closed bar.
        `vwap` and `volume_ratio` come from MarketState (NaN-safe)."""
        day = bar.ts.date()
        if day != self._date:
            self._reset(day)
        phase = self._phase(bar.ts)

        if phase == "pre":
            return ORBReading(note="pre-open")

        if phase == "window":
            self._hi = max(self._hi, bar.high)
            self._lo = min(self._lo, bar.low)
            return ORBReading(
                range_high=round(self._hi, 2), range_low=round(self._lo, 2),
                range_pts=round(self._hi - self._lo, 2), in_window=True,
                note=f"opening range building: {self._lo:.2f}–{self._hi:.2f}",
            )

        # phase == "open": the range is locked.
        self._locked = True
        if not np.isfinite(self._hi) or not np.isfinite(self._lo):
            return ORBReading(locked=True, note="no opening range captured")

        rng = round(self._hi - self._lo, 2)
        base = ORBReading(
            range_high=round(self._hi, 2), range_low=round(self._lo, 2),
            range_pts=rng, locked=True,
        )

        broke_up = bar.close > self._hi and not self._fired_up
        broke_dn = bar.close < self._lo and not self._fired_dn
        if not (broke_up or broke_dn):
            inside = self._lo <= bar.close <= self._hi
            note = "inside range" if inside else "beyond range (already fired)"
            return ORBReading(**_as_dict(base), note=note)

        direction = Direction.BULLISH if broke_up else Direction.BEARISH
        volume_ok = np.isfinite(volume_ratio) and volume_ratio >= self.cfg.vol_confirm_ratio
        if np.isfinite(vwap):
            vwap_ok = bar.close > vwap if broke_up else bar.close < vwap
        else:
            vwap_ok = not self.cfg.require_vwap     # no VWAP yet → don't block
        active = volume_ok and (vwap_ok or not self.cfg.require_vwap)

        if active:                                  # one breakout per side per day
            if broke_up:
                self._fired_up = True
            else:
                self._fired_dn = True

        entry, targets, stop, _ = self._plan(direction)
        note = _breakout_note(direction, entry, rng, targets, volume_ok, vwap_ok, active)
        return ORBReading(
            range_high=round(self._hi, 2), range_low=round(self._lo, 2),
            range_pts=rng, locked=True, direction=direction,
            volume_ok=volume_ok, vwap_ok=vwap_ok, entry=round(entry, 2),
            targets=targets, stop=round(stop, 2), active=active, note=note,
        )


def _as_dict(r: ORBReading) -> dict:
    return {
        "range_high": r.range_high, "range_low": r.range_low,
        "range_pts": r.range_pts, "locked": r.locked,
    }


def _breakout_note(direction, entry, rng, targets, volume_ok, vwap_ok, active) -> str:
    tgt = " / ".join(f"{t:.0f}" for t in targets)
    flags = []
    flags.append("vol ✓" if volume_ok else "vol ✗")
    flags.append("vwap ✓" if vwap_ok else "vwap ✗")
    head = f"{direction.value} ORB breakout @ {entry:.0f} (range {rng:.0f}pt)"
    return f"{head} | targets {tgt} | {' '.join(flags)} | " + ("ACTIVE" if active else "filtered")
