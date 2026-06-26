"""
GEX-as-primary-frame — the persistent, spatial map. Pure logic + a thin stateful
wrapper. No network, no awaits.

`gex.py` stays pure/stateless: it turns a chain into a `GexProfile` (walls, zero-Γ,
regime). This module maintains that structure *across polls* and makes it SPATIAL and
PRIMARY:

  * GexMapTracker  — stateful, updated once per chain poll. Tracks each wall's
    PERSISTENCE (a wall that reappears across polls is stronger; a fresh one is
    provisional) and price's heading, then builds…
  * GexMapState    — where price sits in the gamma envelope: the nearest wall above /
    below + distances, the wall-bounded CELL it occupies, and a position_state in
    {PINNED_AT_WALL, IN_OPEN_SPACE, APPROACHING_WALL, APPROACHING_FLIP,
    OVERSHOT_ENVELOPE}. Carries…
  * GexPrior       — the GEX-derived directional prior, emitted from the map ALONE
    (before any candle/trigger): what the structure expects price to do, and a pure,
    SYMMETRIC `assess(direction, …)` that scores any candidate's location/agreement
    into {PROMOTE, OK, OFF}.

CRITICAL FRAMING: GEX is a MAP, not a trigger. It says WHERE reactions are likely and
WHETHER a setup agrees with structure; the interaction signals (cross, displacement,
flow inflection, CD divergence, approach-arm) still own the entry TIMING. A bare
GEX-level touch never fires an alert. The map is consulted first and gates/scores
everything; it does not pull the trigger.

The prior is SYMMETRIC by construction — positive gamma fades BOTH walls (short into
the call wall, long into the put wall) depending only on where price sits, never a
baked-in directional bias. Any short/vol lean is a separate vanna overlay kept distinct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from helenus.config import CONFIG
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Direction, TriggerType

# Wall as carried through the map: (strike, net_gex, persistence-in-polls).
Wall = tuple[float, float, int]

# The momentum edges get the relaxed ~4pt room floor (we want the smaller base hits);
# everything else holds the stricter ~8pt. Mirrors the analyst's graduated room rule.
MOMENTUM_TRIGGERS = frozenset({
    TriggerType.EMA_IGNITION,
    TriggerType.DISPLACEMENT,
    TriggerType.ORB_BREAKOUT,
    TriggerType.RANGE_EXPANSION,
    TriggerType.ES_LEAD,
})

# Triggers that ARE the map move itself — they define a structural state change rather
# than reacting at a location, so the map never suppresses them. A regime flip crosses
# the pivot the prior is built on; a CD divergence IS the absorption read that overrides
# wall defense. Exempt from both the pre-Claude and post-verdict map OFF.
MAP_EXEMPT_TRIGGERS = frozenset({
    TriggerType.REGIME_FLIP,
    TriggerType.CD_REVERSAL,
})


def is_momentum_trigger(trigger: TriggerType) -> bool:
    return trigger in MOMENTUM_TRIGGERS


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _opposite(d: Direction) -> Direction:
    return Direction.BEARISH if d is Direction.BULLISH else Direction.BULLISH


def _wall_label(wall: Wall | None) -> str:
    """A wall's plain label — call walls are resistance (net>0), put walls support."""
    if wall is None:
        return "n/a"
    strike, net, _persist = wall
    kind = "CallWall" if net >= 0 else "PutWall"
    return f"{kind} {strike:.0f}"


# --------------------------------------------------------------------------- #
# The directional prior
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MapAssessment:
    """The map's verdict on a candidate at this location, given its direction."""
    verdict: str                # PROMOTE | OK | OFF
    agrees: bool                # direction agrees with the structural prior
    reasons: list[str]

    @property
    def is_off(self) -> bool:
        return self.verdict == "OFF"


@dataclass(frozen=True)
class GexPrior:
    """The GEX-derived directional prior — emitted from the map alone, BEFORE any
    trigger. `favored_direction` is the reaction the structure expects *at this
    instant* (None = genuinely two-sided / no lean). It is SYMMETRIC across the
    session: in positive gamma it favors a fade of whichever wall price is nearer, so
    the lean flips with location rather than carrying a fixed bias."""
    expected_behavior: str      # MEAN_REVERT | FADE_EXTENSION | WALL_DEFENSE |
                                # FLIP_ACCELERATION | TREND_CONTINUATION | NONE
    favored_direction: Direction | None
    position_state: str
    note: str
    # Directional structure the assessor needs (the wall in each direction + its
    # persistence) so it can price "into a defended/exhausted wall with no room".
    wall_above: Wall | None
    wall_below: Wall | None

    # -- measurement-friendly coarse tag (ignores room/absorption) ---------- #
    def agreement(self, direction: Direction) -> str:
        """AGREES / AGAINST / NEUTRAL — the coarse directional tag the journal grades
        by (does 'agrees-with-prior' actually outperform 'against'?)."""
        if self.favored_direction is None:
            return "NEUTRAL"
        return "AGREES" if direction == self.favored_direction else "AGAINST"

    # -- the gate ----------------------------------------------------------- #
    def assess(
        self,
        direction: Direction,
        *,
        is_momentum: bool = False,
        room_pts: float | None = None,
        stacked_same_side: bool = False,
        absorption: bool = False,
    ) -> MapAssessment:
        """Score a candidate's location + agreement with the prior. Pure.

        `room_pts` is the room to the next opposing magnet IN the entry direction;
        `is_momentum` relaxes the room floor to ~4pt (an EMA ignition / displacement)
        vs ~8pt otherwise; `stacked_same_side` forces the full ~8pt regardless (a
        stacked same-side wall cluster absorbs the move); `absorption` is an aligned
        CD-divergence read that flips a defended wall (the wall is being eaten, so the
        break-through now agrees).

        Returns PROMOTE (agrees + room + favorable), OK (neutral / tolerable), or OFF
        (into a defended wall / against the prior / no room). OFF means OFF — the
        caller suppresses the alert, it does not merely lower confidence.
        """
        cfg = CONFIG.gexmap
        reasons: list[str] = []

        floor = (
            cfg.stacked_wall_room_pts if stacked_same_side
            else cfg.momentum_room_pts if is_momentum
            else cfg.default_room_pts
        )
        room_ok = room_pts is None or room_pts >= floor

        into_wall = self.wall_above if direction is Direction.BULLISH else self.wall_below
        into_persistence = into_wall[2] if into_wall is not None else 0

        fav = self.favored_direction
        beh = self.expected_behavior

        # Absorption flips a defended wall: the gamma wall is being eaten, so a
        # break THROUGH it now agrees with the (failing) structure, not against it.
        if beh == "WALL_DEFENSE" and absorption and fav is not None:
            fav = _opposite(fav)
            reasons.append("CD absorption: defended wall being eaten — break-through agrees")

        # Hard exhaustion / defended-wall gate (the room rule into stacked walls +
        # re-test fatigue, map-aware): a same-direction push INTO a strongly-held wall
        # with no room is the graded stacked-wall failure → OFF, unless absorption.
        if (
            not room_ok
            and into_wall is not None
            and into_persistence >= cfg.persistence_strong
            and not absorption
        ):
            rp = "?" if room_pts is None else f"{room_pts:.1f}pt"
            reasons.append(
                f"into strongly-held {_wall_label(into_wall)} ({rp}, "
                f"persistence {into_persistence}) — exhausted/defended, no room"
            )
            return MapAssessment("OFF", False, reasons)

        if fav is None:
            reasons.append(f"{beh.lower()} but no directional lean ({self.position_state.lower()})")
            return MapAssessment("OK", False, reasons)

        if direction == fav:
            reasons.append(f"{direction.value} agrees with the {beh.lower()} prior")
            if room_ok:
                return MapAssessment("PROMOTE", True, reasons)
            rp = "?" if room_pts is None else f"{room_pts:.1f}pt"
            reasons.append(f"…but thin room ({rp} < {floor:.0f}pt floor)")
            return MapAssessment("OK", True, reasons)

        # Against the prior.
        reasons.append(
            f"{direction.value} against the {beh.lower()} prior (favored {fav.value})"
        )
        # Fading an accelerating flip, fading a trend, or breaking a defended wall is OFF.
        if beh in ("FLIP_ACCELERATION", "TREND_CONTINUATION", "WALL_DEFENSE"):
            return MapAssessment("OFF", False, reasons)
        # A mean-revert / fade prior: 'against' = a momentum continuation INTO the wall.
        # OFF when there's no room (chasing into a magnet), tolerable (OK) with room.
        if not room_ok:
            rp = "?" if room_pts is None else f"{room_pts:.1f}pt"
            reasons.append(f"continuation into the wall with no room ({rp} < {floor:.0f}pt)")
            return MapAssessment("OFF", False, reasons)
        return MapAssessment("OK", False, reasons)


# --------------------------------------------------------------------------- #
# The spatial state
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GexMapState:
    spot: float
    regime: str
    zero_gamma: float | None
    total_net_gex: float
    nearest_wall_above: Wall | None
    nearest_wall_below: Wall | None
    dist_above_pts: float | None
    dist_below_pts: float | None
    envelope: tuple[float, float] | None   # (lowest wall strike, highest wall strike)
    cell: str                              # human label of the zone price occupies
    position_state: str
    prior: GexPrior
    # ≥2 walls clustered within stacked_wall_span_pts on that side — heavy gamma that
    # absorbs a move into it (the graded stacked-put-wall failure). Keeps the full
    # ~8pt room requirement even for a momentum edge.
    stacked_above: bool = False
    stacked_below: bool = False

    def gate(
        self,
        direction: Direction,
        *,
        is_momentum: bool = False,
        absorption: bool = False,
    ) -> MapAssessment:
        """Score a candidate's direction against the map at this location — the
        single entry point both the pre-Claude veto (bot) and the post-verdict OFF
        (analyst) call. Room is the distance to the next opposing GAMMA magnet in the
        entry direction; the stacked-wall flag and absorption are read off the state."""
        room = self.dist_above_pts if direction is Direction.BULLISH else self.dist_below_pts
        stacked = self.stacked_above if direction is Direction.BULLISH else self.stacked_below
        return self.prior.assess(
            direction,
            is_momentum=is_momentum,
            room_pts=room,
            stacked_same_side=stacked,
            absorption=absorption,
        )

    def snapshot(self) -> dict[str, Any]:
        """The leading block handed to Claude — the map read FIRST, before the rest."""
        return {
            "regime": self.regime,
            "zero_gamma": round(self.zero_gamma, 1) if self.zero_gamma is not None else None,
            "position_state": self.position_state,
            "cell": self.cell,
            "nearest_wall_above": _wall_label(self.nearest_wall_above)
            if self.nearest_wall_above else None,
            "dist_above_pts": round(self.dist_above_pts, 2)
            if self.dist_above_pts is not None else None,
            "nearest_wall_below": _wall_label(self.nearest_wall_below)
            if self.nearest_wall_below else None,
            "dist_below_pts": round(self.dist_below_pts, 2)
            if self.dist_below_pts is not None else None,
            "wall_above_persistence": self.nearest_wall_above[2]
            if self.nearest_wall_above else None,
            "wall_below_persistence": self.nearest_wall_below[2]
            if self.nearest_wall_below else None,
            "prior": {
                "expected_behavior": self.prior.expected_behavior,
                "favored_direction": self.prior.favored_direction.value
                if self.prior.favored_direction else None,
                "note": self.prior.note,
            },
        }


# --------------------------------------------------------------------------- #
# The stateful tracker
# --------------------------------------------------------------------------- #

class GexMapTracker:
    """Maintains the GEX map across chain polls: wall persistence + price heading,
    then derives the spatial state + prior. Mirrors flow.VannaTracker — `.update()`
    once per chain poll, holding only freshest state."""

    def __init__(self) -> None:
        # Persistence count keyed by rounded strike — how many consecutive polls a
        # wall at that strike has been present.
        self._persistence: dict[float, int] = {}
        self._prev_spot: float | None = None

    def update(self, profile: GexProfile) -> GexMapState:
        cfg = CONFIG.gexmap
        spot = profile.spot

        # Heading from the poll-to-poll spot delta (the chain polls ~15s; the bar
        # tape leads finer, but this is enough to read "approaching" structure).
        heading: Direction | None = None
        if self._prev_spot is not None:
            d = spot - self._prev_spot
            if d > 0.05:
                heading = Direction.BULLISH
            elif d < -0.05:
                heading = Direction.BEARISH
        self._prev_spot = spot

        # --- wall persistence ------------------------------------------------ #
        raw = list(profile.call_walls) + list(profile.put_walls)
        present = {round(s): (s, net) for s, net in raw}
        new_counts: dict[float, int] = {}
        for key in present:
            new_counts[key] = self._persistence.get(key, 0) + 1
        self._persistence = new_counts

        walls: list[Wall] = [
            (s, net, self._persistence[round(s)]) for s, net in present.values()
        ]
        walls.sort(key=lambda w: w[0])

        return self._build_state(spot, profile, walls, heading, cfg)

    # ------------------------------------------------------------------ #
    # State / prior derivation (pure given the resolved walls + heading)
    # ------------------------------------------------------------------ #

    def _build_state(
        self,
        spot: float,
        profile: GexProfile,
        walls: list[Wall],
        heading: Direction | None,
        cfg: Any,
    ) -> GexMapState:
        regime = profile.regime
        zero = profile.zero_gamma

        above = sorted([w for w in walls if w[0] > spot], key=lambda w: w[0])
        below = sorted([w for w in walls if w[0] < spot], key=lambda w: w[0], reverse=True)
        wall_above = above[0] if above else None
        wall_below = below[0] if below else None
        dist_above = (wall_above[0] - spot) if wall_above else None
        dist_below = (spot - wall_below[0]) if wall_below else None
        envelope = (walls[0][0], walls[-1][0]) if walls else None
        span = cfg.stacked_wall_span_pts
        stacked_above = len(above) >= 2 and (above[1][0] - above[0][0]) <= span
        stacked_below = len(below) >= 2 and (below[0][0] - below[1][0]) <= span

        position_state = self._position_state(
            spot, walls, envelope, wall_above, wall_below,
            dist_above, dist_below, zero, heading, cfg,
        )
        cell = self._cell_label(spot, envelope, wall_above, wall_below, position_state)
        prior = self._prior(
            regime, position_state, spot, zero, heading,
            wall_above, wall_below, dist_above, dist_below, envelope, cfg,
        )
        return GexMapState(
            spot=spot,
            regime=regime,
            zero_gamma=zero,
            total_net_gex=profile.total_net_gex,
            nearest_wall_above=wall_above,
            nearest_wall_below=wall_below,
            dist_above_pts=dist_above,
            dist_below_pts=dist_below,
            envelope=envelope,
            cell=cell,
            position_state=position_state,
            prior=prior,
            stacked_above=stacked_above,
            stacked_below=stacked_below,
        )

    @staticmethod
    def _position_state(
        spot, walls, envelope, wall_above, wall_below,
        dist_above, dist_below, zero, heading, cfg,
    ) -> str:
        if not walls or envelope is None:
            return "IN_OPEN_SPACE"
        if spot > envelope[1] or spot < envelope[0]:
            return "OVERSHOT_ENVELOPE"

        # Pin reads against the nearest wall by ABSOLUTE distance (a wall sitting on
        # spot is neither strictly above nor below, but it still pins).
        nearest_wall_dist = min(abs(w[0] - spot) for w in walls)
        zero_dist = abs(zero - spot) if zero is not None else float("inf")
        if min(nearest_wall_dist, zero_dist) <= cfg.pin_proximity_pts:
            return "PINNED_AT_WALL"

        # Nearest structure AHEAD of price in the heading direction.
        ahead: list[tuple[str, float]] = []
        if heading is Direction.BULLISH:
            if dist_above is not None:
                ahead.append(("wall", dist_above))
            if zero is not None and zero > spot:
                ahead.append(("flip", zero - spot))
        elif heading is Direction.BEARISH:
            if dist_below is not None:
                ahead.append(("wall", dist_below))
            if zero is not None and zero < spot:
                ahead.append(("flip", spot - zero))
        if ahead:
            kind, dist = min(ahead, key=lambda t: t[1])
            if dist <= cfg.approach_pts:
                return "APPROACHING_FLIP" if kind == "flip" else "APPROACHING_WALL"
        return "IN_OPEN_SPACE"

    @staticmethod
    def _cell_label(spot, envelope, wall_above, wall_below, position_state) -> str:
        if envelope is None:
            return "no walls"
        if position_state == "OVERSHOT_ENVELOPE":
            if spot > envelope[1]:
                return f"ABOVE {_wall_label(wall_below)} (overshot)" if wall_below else "above envelope"
            return f"BELOW {_wall_label(wall_above)} (overshot)" if wall_above else "below envelope"
        lo = _wall_label(wall_below) if wall_below else "open"
        hi = _wall_label(wall_above) if wall_above else "open"
        return f"{lo} ↔ {hi}"

    @staticmethod
    def _prior(
        regime, position_state, spot, zero, heading,
        wall_above, wall_below, dist_above, dist_below, envelope, cfg,
    ) -> GexPrior:
        pos_gamma = regime.startswith("POSITIVE")
        behavior = "NONE"
        favored: Direction | None = None
        note = ""

        if regime == "UNKNOWN" or envelope is None:
            note = "no gamma structure — map inactive"

        elif position_state == "OVERSHOT_ENVELOPE":
            above_side = spot > envelope[1]
            if pos_gamma:
                behavior = "FADE_EXTENSION"
                favored = Direction.BEARISH if above_side else Direction.BULLISH
                note = (
                    "overshot the gamma envelope in positive gamma — expect a snap "
                    "back inside (fade the extension)"
                )
            else:
                behavior = "TREND_CONTINUATION"
                favored = Direction.BULLISH if above_side else Direction.BEARISH
                note = (
                    "overshot the gamma envelope in negative gamma — expect "
                    "continuation (the move keeps running)"
                )

        elif position_state == "PINNED_AT_WALL":
            behavior = "WALL_DEFENSE"
            # Pinned at a wall (not merely the zero-Γ knife-edge): the wall defends,
            # so the prior fades AWAY from it. A wall above → fade down; below → up.
            pinned_above = (
                dist_above is not None and dist_above <= cfg.pin_proximity_pts
                and (dist_below is None or dist_above <= dist_below)
            )
            pinned_below = (
                dist_below is not None and dist_below <= cfg.pin_proximity_pts
                and (dist_above is None or dist_below < dist_above)
            )
            if pinned_above:
                favored = Direction.BEARISH
                note = f"pinned at {_wall_label(wall_above)} — expect defense (fade down) unless absorbed"
            elif pinned_below:
                favored = Direction.BULLISH
                note = f"pinned at {_wall_label(wall_below)} — expect defense (fade up) unless absorbed"
            else:
                note = "pinned at the zero-Γ flip — knife-edge, no directional lean"

        elif position_state == "APPROACHING_FLIP":
            behavior = "FLIP_ACCELERATION"
            favored = heading
            note = (
                "approaching the zero-Γ flip with momentum — expect acceleration "
                "into the negative-gamma zone (continuation through the pivot)"
            )

        elif pos_gamma:
            # Open space / approaching a wall in positive gamma: mean-revert, fade the
            # extension into whichever wall is nearer (SYMMETRIC — the lean is set by
            # location, not a fixed bias).
            behavior = "MEAN_REVERT"
            if dist_above is not None and dist_below is not None:
                favored = Direction.BEARISH if dist_above < dist_below else Direction.BULLISH
            elif dist_above is not None:
                favored = Direction.BEARISH
            elif dist_below is not None:
                favored = Direction.BULLISH
            if favored is Direction.BEARISH:
                note = "positive gamma, nearer the call wall — expect mean-reversion down (fade the push up)"
            elif favored is Direction.BULLISH:
                note = "positive gamma, nearer the put wall — expect mean-reversion up (fade the push down)"
            else:
                note = "positive gamma, mid-cell — mean-reverting, no directional lean yet"

        else:
            behavior = "TREND_CONTINUATION"
            favored = heading
            note = (
                "negative gamma in open space — expect continuation/trend; "
                "with-trend breaks run, counter-trend fades fail"
            )

        return GexPrior(
            expected_behavior=behavior,
            favored_direction=favored,
            position_state=position_state,
            note=note,
            wall_above=wall_above,
            wall_below=wall_below,
        )
