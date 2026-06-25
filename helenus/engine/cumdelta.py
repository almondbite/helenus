"""
Cumulative-delta exhaustion on /ES — a LEADING reversal read.

Helenus's reactive triggers (EMA ignition, displacement, ORB, range-expansion,
level-rejection, level-cross) are lagging by construction: they cannot fire until
a move has matured, so price often continues only a little before reversing. The
graded failure cluster in journal/lessons.md is exactly that — afternoon shorts
fired repeatedly into the already-held 7340-7350 session-low shelf while buyers
quietly absorbed every break.

Cumulative delta leads price. It is the running sum of *executed/aggressor* /ES
volume — buyers lifting the offer (+) vs sellers hitting the bid (−). When price
prints a new session extreme but cumulative delta does NOT confirm it, the
aggressor behind the move is being absorbed and the move is spent BEFORE price
reverses. That single read gives two outputs:

  (a) a VETO / conviction-cap on a reactive trigger firing INTO absorption (a
      short into a low that keeps holding on rising delta — the 7340-7350 cluster),
  (b) a reversal ARM when the divergence appears at a structural level (the early,
      anticipatory entry the reactive triggers can't produce).

level_one_futures carries no per-trade aggressor tag (that lives in L2 / time &
sales), so each executed-volume increment is signed with the standard QUOTE RULE
+ TICK-RULE fallback: last >= ask → buy, last <= bid → sell, else uptick → buy /
downtick → sell (carry the prior sign on a flat tick with no quote help). This is
a well-understood approximation of cumulative delta from level-one data.

Pure and deterministic — no I/O, no awaits. Fed one /ES level-one tick at a time
by data/schwab_stream.py; the bot pulls a reading per bar behind the stream's
staleness gate. CD is stream-only by construction (the 30s REST poll is far too
coarse for executed-flow classification), so when the /ES stream is stale or
disabled there is simply no reading and every consumer no-ops.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from helenus.config import CONFIG, CumDeltaConfig
from helenus.engine.scan2 import Direction


# --------------------------------------------------------------------------- #
# Tick classification (pure)
# --------------------------------------------------------------------------- #

def _classify_sign(
    last: float, prev_last: float | None, bid: float, ask: float, prev_sign: float
) -> float:
    """+1 buy / -1 sell / prev_sign carry for one executed-volume increment.

    Quote rule first (last at/through the offer = buy, at/through the bid = sell);
    tick rule when last sits inside the spread (uptick = buy, downtick = sell);
    on a flat tick with no quote help, carry the prior aggressor sign."""
    if ask == ask and ask > 0 and last >= ask:        # not NaN, at/through offer
        return 1.0
    if bid == bid and bid > 0 and last <= bid:        # at/through bid
        return -1.0
    if prev_last is not None and prev_last == prev_last:
        if last > prev_last:
            return 1.0
        if last < prev_last:
            return -1.0
    return prev_sign


def classify_tick(
    last: float,
    prev_last: float | None,
    bid: float,
    ask: float,
    vol_delta: float,
    prev_sign: float = 0.0,
) -> float:
    """Signed executed volume for one tick: the quote/tick-rule sign × the volume
    increment. Zero when there is no fresh volume (a quote-only delta tick)."""
    if not (vol_delta == vol_delta) or vol_delta <= 0:   # NaN or no fresh volume
        return 0.0
    return _classify_sign(last, prev_last, bid, ask, prev_sign) * vol_delta


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CumDeltaReading:
    cum_delta: float                 # session running signed executed volume
    session_high: float | None
    session_low: float | None
    cd_at_high: float | None         # delta high-water mark recorded at the highs
    cd_at_low: float | None          # delta low-water mark recorded at the lows
    bullish_absorption: bool         # at the low shelf, sellers absorbed (down spent)
    bearish_absorption: bool         # at the high shelf, buyers absorbed (up spent)
    divergence: float                # magnitude of the absorbing-side CD divergence
    note: str

    @property
    def arm_direction(self) -> Direction | None:
        """Which way the early reversal arms: a down-move absorbed → BULLISH; an
        up-move absorbed → BEARISH. None when no clean divergence."""
        if self.bullish_absorption:
            return Direction.BULLISH
        if self.bearish_absorption:
            return Direction.BEARISH
        return None

    @property
    def veto_direction(self) -> Direction | None:
        """The exhausted-move continuation to veto — the opposite of the arm.
        Bullish absorption (down spent) vetoes BEARISH shorts; bearish absorption
        (up spent) vetoes BULLISH longs."""
        arm = self.arm_direction
        if arm is Direction.BULLISH:
            return Direction.BEARISH
        if arm is Direction.BEARISH:
            return Direction.BULLISH
        return None

    def vetoes(self, direction: Direction) -> bool:
        """True if `direction` would be chasing an exhausted move into absorption."""
        return self.veto_direction is not None and self.veto_direction == direction


# --------------------------------------------------------------------------- #
# Tracker (stateful — fed per /ES tick)
# --------------------------------------------------------------------------- #

class CumDeltaTracker:
    """Folds successive /ES level-one ticks into a session cumulative-delta series
    and detects price/delta divergence at the session extremes (absorption).

    Mirrors the stateful trackers elsewhere (ESTracker / VannaTracker): the
    cumulative-volume field is turned into per-tick executed flow (interval delta,
    clamped at 0 so the daily reset can't fake a surge), each increment is signed,
    and the running sum is compared against the delta recorded when price was last
    at the session high / low.

    `cd_at_high` is the delta HIGH-water mark observed while price sat at the
    session high; `cd_at_low` the LOW-water mark at the session low. A price
    extreme reached (or re-tagged) without the delta extending its matching
    water-mark is divergence: the aggressor is being absorbed."""

    def __init__(self, cfg: CumDeltaConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.cumdelta
        self._prev_cum_vol: float | None = None
        self._prev_last: float | None = None
        self._prev_sign: float = 0.0
        self.cum_delta: float = 0.0
        self.session_high: float | None = None
        self.session_low: float | None = None
        # Delta reference at the CURRENT shelf (raised/lowered as price hugs it —
        # confirmation), plus the reference carried from the PRIOR shelf (so a new
        # extreme made on weaker delta than the last one reads as divergence).
        self.cd_at_high: float | None = None
        self.cd_at_low: float | None = None
        self.prior_cd_at_high: float | None = None
        self.prior_cd_at_low: float | None = None
        self.updated: float = 0.0

    def _reset_session(self) -> None:
        self.cum_delta = 0.0
        self.session_high = self.session_low = None
        self.cd_at_high = self.cd_at_low = None
        self.prior_cd_at_high = self.prior_cd_at_low = None
        self._prev_last = None
        self._prev_sign = 0.0

    def observe(
        self,
        last: float,
        bid: float = float("nan"),
        ask: float = float("nan"),
        total_volume: float = float("nan"),
        now: float | None = None,
    ) -> None:
        """Fold one /ES level-one tick. `last`/`total_volume` must be present
        (finite) to classify; a quote-only delta tick (no price/volume) is a no-op."""
        self.updated = now if now is not None else time.monotonic()
        if not (last == last) or not (total_volume == total_volume):   # NaN guard
            return

        # Interval executed volume = delta of cumulative volume. A sharp drop is a
        # daily/session reset → start a fresh session rather than clamp forever.
        if self._prev_cum_vol is None:
            vol_delta = 0.0
        elif total_volume < self._prev_cum_vol * 0.5:
            self._reset_session()
            vol_delta = 0.0
        else:
            vol_delta = max(0.0, total_volume - self._prev_cum_vol)
        self._prev_cum_vol = total_volume

        signed = classify_tick(last, self._prev_last, bid, ask, vol_delta, self._prev_sign)
        if signed > 0:
            self._prev_sign = 1.0
        elif signed < 0:
            self._prev_sign = -1.0
        self.cum_delta += signed
        self._prev_last = last

        tol = self.cfg.retag_tolerance_pts
        # HIGH shelf. A strictly new high starts a fresh shelf: snapshot the prior
        # shelf's delta reference (for the new-high-on-weaker-delta divergence) and
        # reset the current reference to now. While merely hugging the high, a
        # rising delta confirms it (raise the reference); a stalled delta is the
        # absorption the reading measures against the reference.
        if self.session_high is None or last > self.session_high:
            if self.session_high is not None:
                self.prior_cd_at_high = self.cd_at_high
            self.session_high = last
            self.cd_at_high = self.cum_delta
        elif last >= self.session_high - tol:
            if self.cd_at_high is None or self.cum_delta > self.cd_at_high:
                self.cd_at_high = self.cum_delta
        # LOW shelf — mirror (a falling delta confirms the lows).
        if self.session_low is None or last < self.session_low:
            if self.session_low is not None:
                self.prior_cd_at_low = self.cd_at_low
            self.session_low = last
            self.cd_at_low = self.cum_delta
        elif last <= self.session_low + tol:
            if self.cd_at_low is None or self.cum_delta < self.cd_at_low:
                self.cd_at_low = self.cum_delta

    def reading(self) -> CumDeltaReading:
        cfg = self.cfg
        tol = cfg.retag_tolerance_pts
        last = self._prev_last

        hi, lo = self.session_high, self.session_low
        # "At a shelf" requires price unambiguously at ONE extreme — within tol of
        # it AND more than tol from the other — so a sub-tol range (price equally
        # near both) arms neither side.
        at_high = (
            last is not None and hi is not None and last >= hi - tol
            and (lo is None or last - lo > tol)
        )
        at_low = (
            last is not None and lo is not None and last <= lo + tol
            and (hi is None or hi - last > tol)
        )

        bearish = bullish = False
        bear_div = bull_div = 0.0
        # Bearish absorption: at the high shelf with delta below either the current
        # shelf's reference (a hold that's bleeding) or the prior high's reference
        # (a new high made on weaker buying). Take the larger of the two.
        if at_high:
            cands = [r - self.cum_delta for r in (self.cd_at_high, self.prior_cd_at_high) if r is not None]
            bear_div = max(cands) if cands else 0.0
            bearish = bear_div >= cfg.min_divergence
        # Bullish absorption — mirror at the low shelf (the 7340-7350 held low).
        if at_low:
            cands = [self.cum_delta - r for r in (self.cd_at_low, self.prior_cd_at_low) if r is not None]
            bull_div = max(cands) if cands else 0.0
            bullish = bull_div >= cfg.min_divergence

        # A tiny range can sit at both shelves at once — take the stronger
        # divergence, and call it neither on a tie so we never arm both ways.
        if bullish and bearish:
            if bull_div > bear_div:
                bearish = False
            elif bear_div > bull_div:
                bullish = False
            else:
                bullish = bearish = False

        if bullish:
            divergence = bull_div
            note = (
                f"Bullish absorption: price holding the {self.session_low:.2f} low but "
                f"cum-delta {self.cum_delta:,.0f} is {bull_div:,.0f} above its low-water "
                f"mark — sellers absorbed, down-move spent."
            )
        elif bearish:
            divergence = bear_div
            note = (
                f"Bearish absorption: price holding the {self.session_high:.2f} high but "
                f"cum-delta {self.cum_delta:,.0f} is {bear_div:,.0f} below its high-water "
                f"mark — buyers absorbed, up-move spent."
            )
        else:
            divergence = 0.0
            note = f"Cum-delta {self.cum_delta:,.0f}; no divergence at the session extremes."

        return CumDeltaReading(
            cum_delta=round(self.cum_delta, 1),
            session_high=self.session_high,
            session_low=self.session_low,
            cd_at_high=round(self.cd_at_high, 1) if self.cd_at_high is not None else None,
            cd_at_low=round(self.cd_at_low, 1) if self.cd_at_low is not None else None,
            bullish_absorption=bullish,
            bearish_absorption=bearish,
            divergence=round(divergence, 1),
            note=note,
        )
