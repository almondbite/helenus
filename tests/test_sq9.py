"""
Offline tests for engine/sq9.py — the Gann Square-of-9 deterministic lattice
(Task 3: confluence-MODIFIER + exit grid; never initiates, never vetoes).

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_sq9.py
    python tests/test_sq9.py

The lattice fixtures are hand-computed from perfect-square anchors, where the
ring arithmetic level(θ) = (√A ± 2·θ/360)² comes out exact:
    anchor 100  (√10): +45°=105.0625 +90°=110.25 +180°=121 +360°=144 / −45°=95.0625 −360°=64
    anchor 7396 (√86): +45°=7439.0625 +90°=7482.25 +360°=7744 / −45°=7353.0625 −360°=7056
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from helenus.config import CONFIG
from helenus.engine.analyst import _assemble_system_prompt, _sq9_block
from helenus.engine.scan2 import Bar, Direction, MarketState
from helenus.engine.sq9 import (
    Sq9Engine,
    Sq9Grid,
    Sq9Level,
    Sq9Reaction,
    Sq9Reading,
    assess,
    detect_reaction,
    exit_grid_note,
    sq9_levels,
)

CFG = CONFIG.sq9
NO_MODIFIER = dataclasses.replace(CFG, modifier_enabled=False)
DISABLED = dataclasses.replace(CFG, enabled=False)

_T0 = dt.datetime(2026, 6, 22, 9, 31)


def _bar(minute: int, c: float, *, o=None, h=None, l=None, v=100.0) -> Bar:
    return Bar(
        ts=_T0 + dt.timedelta(minutes=minute),
        open=float(c if o is None else o),
        high=float(c if h is None else h),
        low=float(c if l is None else l),
        close=float(c),
        volume=v,
    )


def _grid(anchor: float = 7396.0, kind: str = "session_open") -> Sq9Grid:
    return Sq9Grid(anchor=anchor, anchor_kind=kind, levels=tuple(sq9_levels(anchor)))


def _reading(
    grid: Sq9Grid,
    spot: float,
    reaction: Sq9Reaction | None = None,
    counts: dict[float, int] | None = None,
) -> Sq9Reading:
    above = grid.nearest_above(spot)
    below = grid.nearest_below(spot)
    return Sq9Reading(
        grid=grid,
        spot=spot,
        nearest_above=above,
        nearest_below=below,
        dist_above_pts=(above.price - spot) if above else None,
        dist_below_pts=(spot - below.price) if below else None,
        reaction=reaction,
        reaction_counts=counts or {},
    )


# --------------------------------------------------------------------------- #
# Anchor → level hand fixtures
# --------------------------------------------------------------------------- #

def test_levels_anchor_100_exact() -> None:
    by_deg = {lv.degrees: lv.price for lv in sq9_levels(100.0)}
    assert len(by_deg) == 17                       # 8 up + 8 down + the anchor
    assert by_deg[0.0] == 100.0
    assert by_deg[45.0] == 105.0625                # 10.25²
    assert by_deg[90.0] == 110.25                  # 10.5²
    assert by_deg[180.0] == 121.0                  # 11²
    assert by_deg[360.0] == 144.0                  # 12²
    assert by_deg[-45.0] == 95.0625                # 9.75²
    assert by_deg[-90.0] == 90.25                  # 9.5²
    assert by_deg[-360.0] == 64.0                  # 8²


def test_levels_anchor_7396_exact() -> None:
    by_deg = {lv.degrees: lv.price for lv in sq9_levels(7396.0)}
    assert by_deg[45.0] == 7439.0625               # 86.25²
    assert by_deg[90.0] == 7482.25                 # 86.5²
    assert by_deg[360.0] == 7744.0                 # 88²
    assert by_deg[-45.0] == 7353.0625              # 85.75²
    assert by_deg[-360.0] == 7056.0                # 84²


def test_levels_sorted_and_knobs_respected() -> None:
    levels = sq9_levels(7396.0)
    prices = [lv.price for lv in levels]
    assert prices == sorted(prices)
    assert len(sq9_levels(7396.0, rotations=0.5)) == 9      # 4 + 4 + anchor
    assert len(sq9_levels(7396.0, step_degrees=90.0)) == 9  # 4 + 4 + anchor
    assert sq9_levels(0.0) == []


# --------------------------------------------------------------------------- #
# Nearest rings + the exit grid
# --------------------------------------------------------------------------- #

def test_nearest_rings_around_spot() -> None:
    g = _grid()
    assert g.nearest_above(7400.0).price == 7439.0625
    assert g.nearest_below(7400.0).price == 7396.0          # the anchor ring
    assert g.nearest_above(7743.0).price == 7744.0
    assert g.nearest_above(7745.0) is None                   # past the lattice


def test_exit_grid_is_directional() -> None:
    g = _grid()
    tgt, stop = g.exit_grid(Direction.BULLISH, 7400.0)
    assert tgt.price == 7439.0625 and stop.price == 7396.0
    tgt, stop = g.exit_grid(Direction.BEARISH, 7400.0)
    assert tgt.price == 7396.0 and stop.price == 7439.0625


def test_exit_grid_note_text() -> None:
    note = exit_grid_note(_reading(_grid(), 7430.0), Direction.BULLISH)
    assert note is not None
    assert note.startswith("Sq9 exit grid: target 7439.06")
    assert "stop ref 7396.00" in note
    assert "anchor session_open @ 7396.00" in note
    assert exit_grid_note(None, Direction.BULLISH) is None


# --------------------------------------------------------------------------- #
# Reaction vs proximity (reaction/hold > mere proximity)
# --------------------------------------------------------------------------- #

def test_support_reaction_tag_and_hold() -> None:
    # Low tags the +45° ring (7439.0625) within tolerance and the bar closes
    # back above it with a real wick → a support reaction, BULLISH.
    bar = _bar(10, 7444.0, h=7444.5, l=7439.5)
    rx = detect_reaction([bar], _grid(), CFG)
    assert rx is not None and rx.kind == "support"
    assert rx.direction is Direction.BULLISH
    assert rx.level.price == 7439.0625


def test_resistance_reaction_is_the_mirror() -> None:
    bar = _bar(10, 7478.0, h=7482.5, l=7477.5)   # high tags 7482.25, closes off it
    rx = detect_reaction([bar], _grid(), CFG)
    assert rx is not None and rx.kind == "resistance"
    assert rx.direction is Direction.BEARISH
    assert rx.level.price == 7482.25


def test_mere_proximity_is_not_a_reaction() -> None:
    # Near the ring but never tags it → nothing.
    assert detect_reaction([_bar(10, 7444.0, h=7444.0, l=7443.0)], _grid(), CFG) is None
    # Tags it but closes right on the low (no hold wick) → nothing.
    assert detect_reaction([_bar(10, 7440.9, h=7441.0, l=7439.5)], _grid(), CFG) is None


# --------------------------------------------------------------------------- #
# Engine anchoring + session state
# --------------------------------------------------------------------------- #

def _run(engine: Sq9Engine, bars: list[Bar]):
    st = MarketState()
    reading = None
    for b in bars:
        st.push_bar(b)
        reading = engine.on_bar(st)
    return st, reading


def test_session_open_anchor_is_fixed() -> None:
    eng = Sq9Engine(CFG)
    _, r = _run(eng, [_bar(0, 7400.0, o=7396.0), _bar(1, 7420.0), _bar(2, 7450.0)])
    assert r is not None
    assert r.grid.anchor == 7396.0 and r.grid.anchor_kind == "session_open"


def test_prior_close_anchor() -> None:
    eng = Sq9Engine(dataclasses.replace(CFG, anchor="prior_close"))
    st = MarketState(prior_close=7396.0)
    st.push_bar(_bar(0, 7400.0))
    r = eng.on_bar(st)
    assert r is not None and r.grid.anchor == 7396.0


def test_session_low_reanchors_on_new_extreme() -> None:
    eng = Sq9Engine(dataclasses.replace(CFG, anchor="session_low"))
    st = MarketState()
    st.push_bar(_bar(0, 7400.0, l=7396.0))
    r1 = eng.on_bar(st)
    assert r1.grid.anchor == 7396.0
    st.push_bar(_bar(1, 7391.0, l=7390.0))       # new session low → new lattice
    # (close only 1pt off the low — no reaction on this bar, isolating the reset)
    r2 = eng.on_bar(st)
    assert r2.grid.anchor == 7390.0
    assert r2.reaction_counts == {}               # counts belong to the old rings


def test_new_session_resets_anchor_and_counts() -> None:
    eng = Sq9Engine(CFG)
    _run(eng, [_bar(0, 7400.0, o=7396.0)])
    next_day = Bar(
        ts=_T0 + dt.timedelta(days=1), open=7500.0, high=7500.0,
        low=7500.0, close=7500.0, volume=100.0,
    )
    st = MarketState()
    st.push_bar(next_day)
    r = eng.on_bar(st)
    assert r.grid.anchor == 7500.0                # re-anchored to the new open


def test_warm_seeds_the_session_open() -> None:
    eng = Sq9Engine(CFG)
    backfill = [_bar(i, 7400.0 + i, o=7396.0 if i == 0 else None) for i in range(30)]
    eng.warm(backfill)
    st = MarketState()
    st.push_bar(_bar(31, 7440.0))                 # live bar, mid-session restart
    r = eng.on_bar(st)
    assert r.grid.anchor == 7396.0                # same lattice the session had


def test_reaction_counts_accumulate() -> None:
    eng = Sq9Engine(CFG)
    bars = [
        _bar(0, 7430.0, o=7396.0),
        _bar(1, 7444.0, h=7444.5, l=7439.5),      # reaction 1 at 7439.0625
        _bar(2, 7442.0),
        _bar(3, 7445.0, h=7445.5, l=7439.8),      # reaction 2 at the same ring
    ]
    _, r = _run(eng, bars)
    assert r.reaction_counts.get(7439.06) == 2


def test_disabled_engine_returns_none() -> None:
    eng = Sq9Engine(DISABLED)
    _, r = _run(eng, [_bar(0, 7400.0)])
    assert r is None


# --------------------------------------------------------------------------- #
# The modifier (bounded delta; never a veto)
# --------------------------------------------------------------------------- #

def _rx(price: float, degrees: float, direction: Direction) -> Sq9Reaction:
    kind = "support" if direction is Direction.BULLISH else "resistance"
    return Sq9Reaction(direction, Sq9Level(price, degrees), kind, wick_pts=4.0)


def test_aligned_reaction_amplifies() -> None:
    r = _reading(_grid(), 7441.0, reaction=_rx(7439.0625, 45.0, Direction.BULLISH))
    delta, note = assess(r, Direction.BULLISH, None, CFG)
    assert delta == CFG.amplify_pts == 6.0
    assert note is not None and "amplify" in note


def test_coincidence_with_structure_adds_the_bonus() -> None:
    r = _reading(_grid(), 7441.0, reaction=_rx(7439.0625, 45.0, Direction.BULLISH))
    delta, note = assess(r, Direction.BULLISH, 7440.0, CFG)   # setup level ≤2pt away
    assert delta == CFG.amplify_pts + CFG.coincide_bonus_pts == 9.0
    assert "coinciding" in note


def test_misaligned_reaction_does_not_amplify() -> None:
    r = _reading(_grid(), 7441.0, reaction=_rx(7439.0625, 45.0, Direction.BULLISH))
    delta, _ = assess(r, Direction.BEARISH, None, CFG)
    assert delta == 0.0                            # (no dampen either: ring below never held)


def test_dampen_needs_a_reacted_ring_not_proximity() -> None:
    # Spot 4pt under the +90° ring; a long INTO it dampens only once that ring
    # has actually HELD this session.
    grid = _grid()
    proximity_only = _reading(grid, 7478.25)
    assert assess(proximity_only, Direction.BULLISH, None, CFG)[0] == 0.0
    reacted = _reading(grid, 7478.25, counts={7482.25: 1})
    delta, note = assess(reacted, Direction.BULLISH, None, CFG)
    assert delta == -CFG.dampen_pts == -4.0
    assert "dampen" in note and "held 1×" in note


def test_amplify_and_dampen_can_net() -> None:
    # Fresh aligned reaction at the ring below + pushing into a held ring above.
    r = _reading(
        _grid(), 7478.25,
        reaction=_rx(7439.0625, 45.0, Direction.BULLISH),
        counts={7482.25: 2},
    )
    delta, _ = assess(r, Direction.BULLISH, None, CFG)
    assert delta == CFG.amplify_pts - CFG.dampen_pts == 2.0


def test_modifier_disabled_is_zero_but_grid_survives() -> None:
    r = _reading(_grid(), 7441.0, reaction=_rx(7439.0625, 45.0, Direction.BULLISH))
    delta, note = assess(r, Direction.BULLISH, None, NO_MODIFIER)
    assert delta == 0.0 and note is None
    assert r.nearest_above is not None             # the read-only roles are intact
    assert exit_grid_note(r, Direction.BULLISH) is not None


def test_assess_never_suppresses() -> None:
    # Structural: the worst case is the bounded dampen — there is no OFF/veto
    # value in the return contract, just a float delta.
    grid = _grid()
    worst = _reading(grid, 7478.25, counts={7482.25: 9})
    delta, _ = assess(worst, Direction.BULLISH, None, CFG)
    assert delta == -CFG.dampen_pts               # bounded, not a kill


# --------------------------------------------------------------------------- #
# Snapshot + prompt surfaces
# --------------------------------------------------------------------------- #

def test_sq9_block_shape() -> None:
    r = _reading(
        _grid(), 7441.0,
        reaction=_rx(7439.0625, 45.0, Direction.BULLISH),
        counts={7439.06: 1},
    )
    block = _sq9_block(r)
    assert block["available"] is True
    assert block["anchor"] == 7396.0 and block["anchor_kind"] == "session_open"
    assert block["nearest_above"]["price"] == 7482.25
    assert block["nearest_below"]["price"] == 7439.06
    assert block["reaction"]["kind"] == "support"
    assert block["session_reactions"] == {"7439.06": 1}
    assert _sq9_block(None) == {"available": False}


def test_prompt_carries_sq9_only_when_enabled() -> None:
    with_sq9 = _assemble_system_prompt(False, True, True)
    assert "GANN SQUARE-OF-9" in with_sq9
    assert "MODIFIER ONLY" in with_sq9
    assert "NEVER fire a signal" in with_sq9
    without = _assemble_system_prompt(False, True, False)
    assert "GANN SQUARE-OF-9" not in without
    legacy = _assemble_system_prompt(True, False, False)   # the original prompt
    assert "GANN SQUARE-OF-9" not in legacy


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
