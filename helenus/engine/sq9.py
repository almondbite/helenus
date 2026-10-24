"""
Gann Square-of-9 — a DETERMINISTIC LEVEL LATTICE. Pure arithmetic, no I/O.

Two honest roles, and no others (see Sq9Config):

  (a) CONFLUENCE MODIFIER — a ring level only AMPLIFIES or DAMPENS a signal
      that originated elsewhere, when price REACTS at the level (tag-and-hold
      with a real wick; mere proximity never counts). Coincidence with other
      structure (a ring landing on an MSS break / charm wall / session extreme)
      is what gives a ring its value — that's the stronger shelf. Sq9 NEVER
      initiates (its levels are deliberately NOT in scan2's key-level grid, so
      no trigger can fire at one) and NEVER vetoes (assess() returns a bounded
      delta; no suppression path exists in this module).
  (b) EXIT / TARGET GRID — the next ring above/below an open position as a
      pre-computed, deterministic profit-target and structural-stop reference:
      mechanical mid-trade references that need no judgment.

The math: on the Square-of-9 spiral a full 360° rotation advances √price by 2,
so the ring level at a signed rotation θ from anchor A is

    level(θ) = (√A + 2·θ/360)²        (θ < 0 spirals down)

Reproducible from (anchor, step_degrees, rotations) alone. Gann angles/fans
(anchor+scale dependent) and Gann time cycles are deliberately NOT implemented.

Layout mirrors the other engines: pure functions + frozen dataclasses, and a
thin stateful Sq9Engine (bot-held) that owns the session anchor + per-ring
reaction counts.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from helenus.config import CONFIG, Sq9Config
from helenus.engine.scan2 import Bar, Direction, MarketState


# --------------------------------------------------------------------------- #
# Pure lattice math
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Sq9Level:
    price: float
    degrees: float          # signed rotation from the anchor (0 = the anchor)


def sq9_levels(
    anchor: float, step_degrees: float = 45.0, rotations: float = 1.0
) -> list[Sq9Level]:
    """The ring lattice around `anchor`: levels every `step_degrees`, out to
    ±`rotations`×360°, plus the anchor itself. Sorted ascending by price.
    Pure — level(θ) = (√anchor + 2·θ/360)²."""
    if anchor <= 0:
        return []
    root = math.sqrt(anchor)
    steps = int(round(rotations * 360.0 / step_degrees))
    out: list[Sq9Level] = []
    for i in range(-steps, steps + 1):
        deg = i * step_degrees
        r = root + 2.0 * deg / 360.0
        if r <= 0:
            continue  # a deep-down rotation past zero has no price
        out.append(Sq9Level(price=r * r, degrees=deg))
    out.sort(key=lambda lv: lv.price)
    return out


@dataclass(frozen=True)
class Sq9Grid:
    anchor: float
    anchor_kind: str                     # session_open | prior_close | session_low | session_high
    levels: tuple[Sq9Level, ...]         # ascending

    def nearest_above(self, price: float) -> Sq9Level | None:
        return next((lv for lv in self.levels if lv.price > price), None)

    def nearest_below(self, price: float) -> Sq9Level | None:
        return next((lv for lv in reversed(self.levels) if lv.price < price), None)

    def exit_grid(
        self, direction: Direction, price: float
    ) -> tuple[Sq9Level | None, Sq9Level | None]:
        """(profit-target ring, structural-stop-reference ring) for a position at
        `price`: the next ring IN the trade direction and the first ring against
        it. Deterministic — the exit-discipline references."""
        if direction is Direction.BULLISH:
            return self.nearest_above(price), self.nearest_below(price)
        return self.nearest_below(price), self.nearest_above(price)


# --------------------------------------------------------------------------- #
# Reaction detection (reaction/hold at a ring > mere proximity)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Sq9Reaction:
    direction: Direction     # the reaction's implied lean (support hold = BULLISH)
    level: Sq9Level
    kind: str                # "support" | "resistance"
    wick_pts: float


def detect_reaction(
    bars: list[Bar], grid: Sq9Grid, cfg: Sq9Config | None = None
) -> Sq9Reaction | None:
    """Did the CURRENT bar genuinely react at a ring? Support: the low TAGS a
    ring (within tolerance_pts) and the bar closes back at/above it with a real
    wick (≥ reaction_wick_pts). Resistance mirror on the high. Proximity without
    the tag-and-hold never counts. Returns the stronger reaction if both sides
    somehow qualify on one huge bar. Pure."""
    cfg = cfg or CONFIG.sq9
    if not bars:
        return None
    cur = bars[-1]
    best: Sq9Reaction | None = None
    for lv in grid.levels:
        held_support = (
            abs(cur.low - lv.price) <= cfg.tolerance_pts
            and cur.close >= lv.price
            and (cur.close - cur.low) >= cfg.reaction_wick_pts
        )
        held_resist = (
            abs(cur.high - lv.price) <= cfg.tolerance_pts
            and cur.close <= lv.price
            and (cur.high - cur.close) >= cfg.reaction_wick_pts
        )
        if held_support:
            cand = Sq9Reaction(Direction.BULLISH, lv, "support", cur.close - cur.low)
        elif held_resist:
            cand = Sq9Reaction(Direction.BEARISH, lv, "resistance", cur.high - cur.close)
        else:
            continue
        if best is None or cand.wick_pts > best.wick_pts:
            best = cand
    return best


# --------------------------------------------------------------------------- #
# The per-bar reading + the modifier
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Sq9Reading:
    grid: Sq9Grid
    spot: float
    nearest_above: Sq9Level | None
    nearest_below: Sq9Level | None
    dist_above_pts: float | None
    dist_below_pts: float | None
    reaction: Sq9Reaction | None                 # THIS bar's fresh reaction
    reaction_counts: dict[float, int] = field(default_factory=dict)  # ring price -> session count

    def count_at(self, level: Sq9Level | None) -> int:
        if level is None:
            return 0
        return self.reaction_counts.get(round(level.price, 2), 0)


def assess(
    reading: Sq9Reading | None,
    direction: Direction,
    candidate_level_price: float | None,
    cfg: Sq9Config | None = None,
) -> tuple[float, str | None]:
    """The confluence MODIFIER: a bounded confidence delta for a verdict that
    originated elsewhere. Pure; never a veto by construction (the return is a
    delta, and callers clamp — there is no suppression path).

      AMPLIFY  +amplify_pts when THIS bar's reaction at a ring aligns with the
               verdict direction; +coincide_bonus_pts more when that ring also
               coincides (≤ coincide_pts) with the candidate's own level — a
               ring landing on independent structure is the stronger shelf.
      DAMPEN   −dampen_pts when the verdict pushes INTO the nearest opposing
               ring within dampen_within_pts AND that ring has already REACTED
               this session (reaction_counts ≥ 1). Proximity alone never dampens.

    Weights are PROVISIONAL until the validation phase (see Sq9Config)."""
    cfg = cfg or CONFIG.sq9
    if reading is None or not (cfg.enabled and cfg.modifier_enabled):
        return 0.0, None
    delta = 0.0
    parts: list[str] = []

    rx = reading.reaction
    if rx is not None and rx.direction == direction:
        delta += cfg.amplify_pts
        leg = f"reacted at Sq9 {rx.level.degrees:+.0f}° ({rx.level.price:.2f}, {rx.kind})"
        if (
            candidate_level_price is not None
            and abs(rx.level.price - candidate_level_price) <= cfg.coincide_pts
        ):
            delta += cfg.coincide_bonus_pts
            leg += " coinciding with the setup level"
        parts.append(leg)

    into = (
        reading.nearest_above if direction is Direction.BULLISH
        else reading.nearest_below
    )
    into_dist = (
        reading.dist_above_pts if direction is Direction.BULLISH
        else reading.dist_below_pts
    )
    if (
        into is not None
        and into_dist is not None
        and into_dist <= cfg.dampen_within_pts
        and reading.count_at(into) >= 1
    ):
        delta -= cfg.dampen_pts
        parts.append(
            f"into Sq9 {into.degrees:+.0f}° ({into.price:.2f}, {into_dist:.1f}pt) "
            f"that has held {reading.count_at(into)}× this session"
        )

    if delta == 0.0:
        return 0.0, None
    return delta, "Sq9 " + ("amplify" if delta > 0 else "dampen") + ": " + "; ".join(parts)


def exit_grid_note(reading: Sq9Reading | None, direction: Direction) -> str | None:
    """The deterministic exit references for an alert in `direction` at the
    reading's spot: next ring in the trade direction = profit target, first ring
    against = structural stop reference. Pure string; None when no grid."""
    if reading is None:
        return None
    target, stop = reading.grid.exit_grid(direction, reading.spot)
    if target is None and stop is None:
        return None
    def _fmt(lv: Sq9Level | None) -> str:
        if lv is None:
            return "n/a"
        return f"{lv.price:.2f} ({lv.price - reading.spot:+.1f}pt, {lv.degrees:+.0f}°)"
    return (
        f"Sq9 exit grid: target {_fmt(target)} / stop ref {_fmt(stop)} — "
        f"anchor {reading.grid.anchor_kind} @ {reading.grid.anchor:.2f}"
    )


# --------------------------------------------------------------------------- #
# The stateful engine (held by the bot)
# --------------------------------------------------------------------------- #

class Sq9Engine:
    """Owns the session anchor + per-ring reaction counts. `on_bar` once per tape
    bar → Sq9Reading (None while the anchor source is unresolved or the feature
    is disabled). Anchors once per session; the extreme kinds re-anchor
    deterministically when a new extreme prints (counts reset with the lattice —
    they belong to the old rings)."""

    def __init__(self, cfg: Sq9Config | None = None) -> None:
        self.cfg = cfg or CONFIG.sq9
        self._grid: Sq9Grid | None = None
        self._session: dt.date | None = None
        self._session_open: float | None = None
        self._counts: dict[float, int] = {}

    # -- anchoring ----------------------------------------------------------- #

    def warm(self, bars: list[Bar]) -> None:
        """Seed the session-open anchor from the backfilled tape (bars start at
        09:30, so bars[0].open IS the session open) — a mid-session restart
        rebuilds the same lattice the live session had."""
        if bars:
            self._note_session(bars[0].ts)
            if self._session_open is None:
                self._session_open = bars[0].open

    def _note_session(self, ts: dt.datetime) -> None:
        day = ts.date()
        if day != self._session:
            self._session = day
            self._session_open = None
            self._grid = None
            self._counts = {}

    def _anchor_value(self, state: MarketState) -> float | None:
        kind = self.cfg.anchor
        if kind == "session_open":
            return self._session_open
        if kind == "prior_close":
            return state.prior_close
        if kind == "session_low":
            low = state.session_low
            return float(low) if math.isfinite(low) else None
        if kind == "session_high":
            high = state.session_high
            return float(high) if math.isfinite(high) else None
        return None

    # -- per bar -------------------------------------------------------------- #

    def on_bar(self, state: MarketState) -> Sq9Reading | None:
        if not self.cfg.enabled or not state.bars:
            return None
        cur = state.bars[-1]
        self._note_session(cur.ts)
        if self._session_open is None:
            # First bar of the session (or of this engine's life) sets the open.
            self._session_open = cur.open

        anchor = self._anchor_value(state)
        if anchor is None or anchor <= 0:
            return None
        if self._grid is None or self._grid.anchor != anchor:
            # (Re-)anchor: a new session, or a pivot-kind anchor printed a new
            # extreme. Reaction counts belong to the old lattice — reset.
            self._grid = Sq9Grid(
                anchor=anchor,
                anchor_kind=self.cfg.anchor,
                levels=tuple(sq9_levels(anchor, self.cfg.step_degrees, self.cfg.rotations)),
            )
            self._counts = {}

        reaction = detect_reaction(list(state.bars), self._grid, self.cfg)
        if reaction is not None:
            key = round(reaction.level.price, 2)
            self._counts[key] = self._counts.get(key, 0) + 1

        spot = cur.close
        above = self._grid.nearest_above(spot)
        below = self._grid.nearest_below(spot)
        return Sq9Reading(
            grid=self._grid,
            spot=spot,
            nearest_above=above,
            nearest_below=below,
            dist_above_pts=(above.price - spot) if above else None,
            dist_below_pts=(spot - below.price) if below else None,
            reaction=reaction,
            reaction_counts=dict(self._counts),
        )
