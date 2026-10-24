"""
Offline tests for the Task-1 GEX decoupling (GexConfig.levels_in_signal).

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_gex_decouple.py
    python tests/test_gex_decouple.py

Contract under test: with levels_in_signal=False (the default) GEX LEVELS —
walls, the zero-Γ flip price, room-to-magnet distances, the map gate — are a
MANUAL read-out only and never touch has_signal/direction/confidence: they are
out of the key-level grid (so no trigger fires at a GEX level), out of the
Claude snapshot and prompt, out of the approach pre-load, and the map cannot
veto. Gamma SIGN survives as a label (`gamma_sign`), and the zero-Γ cross is
detectable for the muted info post (detect_regime_flip). Setting the flag True
restores the legacy scored behavior exactly.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from contextlib import contextmanager

import pandas as pd

from helenus.config import CONFIG
from helenus.engine.analyst import _assemble_system_prompt, _snapshot
from helenus.engine.gex import GexProfile
from helenus.engine.gexmap import map_gating_active
from helenus.engine.moc import MocReading
from helenus.engine.scan2 import (
    Bar,
    Candidate,
    Direction,
    MarketState,
    TriggerType,
    detect_approach,
    detect_candidate,
    detect_regime_flip,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@contextmanager
def gex_mode(levels_in_signal: bool):
    """Pin the GEX mode for a block. CONFIG is a frozen dataclass shared by every
    module, so the gex section is swapped in place and restored after."""
    old = CONFIG.gex
    object.__setattr__(
        CONFIG, "gex", dataclasses.replace(old, levels_in_signal=levels_in_signal)
    )
    try:
        yield
    finally:
        object.__setattr__(CONFIG, "gex", old)


@contextmanager
def gexmap_enabled(enabled: bool):
    old = CONFIG.gexmap
    object.__setattr__(CONFIG, "gexmap", dataclasses.replace(old, enabled=enabled))
    try:
        yield
    finally:
        object.__setattr__(CONFIG, "gexmap", old)


def _bar(c: float, h: float | None = None, l: float | None = None, v: float = 100.0) -> Bar:
    return Bar(
        ts=dt.datetime(2026, 6, 24, 11, 0),
        open=c, high=c if h is None else h, low=c if l is None else l, close=c, volume=v,
    )


def _gex(
    spot: float,
    call_walls: list[tuple[float, float]] | None = None,
    put_walls: list[tuple[float, float]] | None = None,
    zero_gamma: float | None = None,
    total_net_gex: float = 0.0,
) -> GexProfile:
    return GexProfile(
        spot=spot, total_net_gex=total_net_gex, zero_gamma=zero_gamma,
        call_walls=call_walls or [], put_walls=put_walls or [], by_strike=pd.DataFrame(),
    )


GEX_LEVEL_LABELS = ("Call Wall", "Put Wall", "Zero-Γ")


def _has_gex_level(labels) -> bool:
    return any(lbl.startswith(GEX_LEVEL_LABELS) for lbl in labels)


# --------------------------------------------------------------------------- #
# Key-level grid — walls/zero-Γ are legacy-only
# --------------------------------------------------------------------------- #

def test_grid_excludes_gex_levels_when_decoupled() -> None:
    st = MarketState(prior_close=7440.0)
    st.push_bar(_bar(7445.0))
    gex = _gex(7445.0, call_walls=[(7460.0, 5e8)], put_walls=[(7430.0, -4e8)], zero_gamma=7442.0)
    with gex_mode(False):
        labels = {lv.label for lv in st.key_levels(7445.0, gex)}
    assert not _has_gex_level(labels)
    # The rest of the grid is untouched: rounds, prior close, session structure, VWAP.
    assert "Prior Close" in labels and "VWAP" in labels
    assert any(lbl.startswith("Round") for lbl in labels)


def test_grid_keeps_gex_levels_in_legacy_mode() -> None:
    st = MarketState(prior_close=7440.0)
    gex = _gex(7445.0, call_walls=[(7460.0, 5e8)], put_walls=[(7430.0, -4e8)], zero_gamma=7442.0)
    with gex_mode(True):
        labels = {lv.label for lv in st.key_levels(7445.0, gex)}
    assert {"Call Wall 7460", "Put Wall 7430", "Zero-Γ"} <= labels


def test_charm_walls_survive_decoupling() -> None:
    # Charm walls are NOT GEX levels — they stay in the grid either way.
    from helenus.engine.charm import CharmProfile
    charm = CharmProfile(
        spot=7445.0, minutes_to_expiry=90.0, net_charm=900.0,
        put_support=1000.0, call_overhead=100.0,
        support_walls=[(7435.0, -1e6)], resistance_walls=[], by_strike=pd.DataFrame(),
    )
    st = MarketState()
    with gex_mode(False):
        labels = {lv.label for lv in st.key_levels(7445.0, _gex(7445.0), charm)}
    assert "Charm Support 7435" in labels


# --------------------------------------------------------------------------- #
# REGIME_FLIP — demoted to an unscored info read when decoupled
# --------------------------------------------------------------------------- #

def _flip_state() -> MarketState:
    st = MarketState()
    st.push_bar(_bar(7405.0))                 # below zero-Γ 7410
    st.push_bar(_bar(7415.0))                 # crosses up
    return st


def test_no_scored_regime_flip_when_decoupled() -> None:
    st = _flip_state()
    with gex_mode(False):
        cand = detect_candidate(st, _gex(7415.0, zero_gamma=7410.0))
    assert cand is None


def test_detect_regime_flip_still_reports_the_cross() -> None:
    st = _flip_state()
    note = detect_regime_flip(st, _gex(7415.0, zero_gamma=7410.0))
    assert note is not None
    assert "up" in note and "POSITIVE_GAMMA" in note


def test_detect_regime_flip_quiet_without_a_cross() -> None:
    st = MarketState()
    st.push_bar(_bar(7420.0))
    st.push_bar(_bar(7425.0))                 # both above the pivot
    assert detect_regime_flip(st, _gex(7425.0, zero_gamma=7410.0)) is None


def test_scored_regime_flip_in_legacy_mode() -> None:
    st = _flip_state()
    with gex_mode(True):
        cand = detect_candidate(st, _gex(7415.0, zero_gamma=7410.0))
    assert cand is not None and cand.trigger is TriggerType.REGIME_FLIP


# --------------------------------------------------------------------------- #
# LEVEL_REJECTION at a GEX wall — fires legacy, silent decoupled
# --------------------------------------------------------------------------- #

def _wall_rejection_state() -> MarketState:
    # Floor 7462 (earlier spike low); the current bar wicks to 7454 — tagging a
    # put wall at 7455 near the range edge — and closes 18pts up. Only the wall
    # is within rejection proximity: the session low (7462) is >3pts from the
    # wick, and no weighty grid level sits nearby.
    st = MarketState()
    st.push_bar(_bar(7490.0, h=7490.0, l=7462.0))
    st.push_bar(_bar(7470.0))
    st.push_bar(_bar(7472.0, h=7473.0, l=7454.0))
    return st


def test_wall_rejection_fires_in_legacy_mode() -> None:
    st = _wall_rejection_state()
    gex = _gex(7472.0, put_walls=[(7455.0, -4e8)])
    with gex_mode(True):
        cand = detect_candidate(st, gex)
    assert cand is not None
    assert cand.trigger is TriggerType.LEVEL_REJECTION
    assert "Put Wall 7455" in cand.reason


def test_wall_rejection_silent_when_decoupled() -> None:
    st = _wall_rejection_state()
    gex = _gex(7472.0, put_walls=[(7455.0, -4e8)])
    with gex_mode(False):
        assert detect_candidate(st, gex) is None


# --------------------------------------------------------------------------- #
# MOC candidates — the GEX pin/overshoot context rides only in legacy mode
# --------------------------------------------------------------------------- #

def _moc_reversal() -> MocReading:
    return MocReading(
        phase="reversal", minutes_to_close=8.0, in_window=True,
        in_reversal_window=True, gex_state="PIN", nearest_wall=7450.0,
        basing_side="put", volume_surge_ratio=2.0,
        reversal_direction=Direction.BEARISH, reversal_active=True,
        note="put side based on surging volume",
    )


def test_moc_candidate_drops_gex_level_when_decoupled() -> None:
    st = MarketState()
    st.push_bar(_bar(7452.0))
    st.push_bar(_bar(7451.0))
    with gex_mode(False):
        cand = detect_candidate(st, _gex(7451.0), moc=_moc_reversal())
    assert cand is not None and cand.trigger is TriggerType.MOC_REVERSAL
    assert cand.level is None


def test_moc_candidate_keeps_gex_level_in_legacy_mode() -> None:
    st = MarketState()
    st.push_bar(_bar(7452.0))
    st.push_bar(_bar(7451.0))
    with gex_mode(True):
        cand = detect_candidate(st, _gex(7451.0), moc=_moc_reversal())
    assert cand is not None and cand.level is not None
    assert cand.level.label == "GEX PIN" and cand.level.price == 7450.0


# --------------------------------------------------------------------------- #
# Approach arms — GEX walls / gamma-sign legs / the map arm are legacy-only
# --------------------------------------------------------------------------- #

def _ascending() -> MarketState:
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7351.0, l=7350.0))
    st.push_bar(_bar(7354.0, l=7352.0))
    st.push_bar(_bar(7357.0, l=7355.0))
    return st


def _descending() -> MarketState:
    st = MarketState(prior_close=7400.0)
    st.push_bar(_bar(7360.0, l=7358.0))
    st.push_bar(_bar(7356.0, l=7355.0))
    st.push_bar(_bar(7353.0, l=7352.0))
    return st


def test_no_wall_arm_when_decoupled() -> None:
    # Legacy arms the bearish fade into the call wall (positive-gamma leg);
    # decoupled the wall isn't a level and the sign leg doesn't count → None.
    st = _ascending()
    gex = _gex(7357.0, call_walls=[(7360.0, 5e8)], zero_gamma=7350.0, total_net_gex=5e8)
    with gex_mode(True):
        assert detect_approach(st, gex, charm=None) is not None
    with gex_mode(False):
        assert detect_approach(st, gex, charm=None) is None


def test_charm_arm_survives_decoupling() -> None:
    # Descending into charm support still arms — charm context isn't GEX.
    from helenus.engine.charm import CharmProfile
    charm = CharmProfile(
        spot=7353.0, minutes_to_expiry=90.0, net_charm=900.0,
        put_support=1000.0, call_overhead=100.0,
        support_walls=[(7350.0, -1e6)], resistance_walls=[], by_strike=pd.DataFrame(),
    )
    st = _descending()
    with gex_mode(False):
        arm = detect_approach(st, _gex(7353.0), charm)
    assert arm is not None and arm.direction is Direction.BULLISH


# --------------------------------------------------------------------------- #
# Map gating — inert when decoupled, even with gexmap.enabled
# --------------------------------------------------------------------------- #

def test_map_gating_active_requires_both_flags() -> None:
    with gex_mode(True), gexmap_enabled(True):
        assert map_gating_active()
    with gex_mode(False), gexmap_enabled(True):
        assert not map_gating_active()
    with gex_mode(True), gexmap_enabled(False):
        assert not map_gating_active()


# --------------------------------------------------------------------------- #
# Claude snapshot — gamma sign only when decoupled
# --------------------------------------------------------------------------- #

def _snapshot_for(levels_in_signal: bool) -> dict:
    st = MarketState(prior_close=7440.0)
    st.push_bar(_bar(7444.0))
    st.push_bar(_bar(7445.0))
    profile = _gex(
        7445.0, call_walls=[(7460.0, 5e8)], put_walls=[(7430.0, -4e8)],
        zero_gamma=7442.0, total_net_gex=5e8,
    )
    cand = Candidate(trigger=TriggerType.VOLUME_CONFIRMATION, level=None, reason="test")
    with gex_mode(levels_in_signal):
        return _snapshot(
            st, profile, None, None, None, None, None, None,
            _moc_reversal(), None, {}, cand,
        )


def test_snapshot_gex_block_is_sign_only_when_decoupled() -> None:
    snap = _snapshot_for(False)
    assert snap["gex"] == {"gamma_sign": "POSITIVE_GAMMA"}
    # No wall levels in the key-level grid handed to Claude either.
    assert not _has_gex_level(lv["label"] for lv in snap["key_levels"])
    # The map block is off regardless of gexmap state (none passed here).
    assert snap["gex_map"] == {"available": False}
    # MOC context sheds the level-derived pin/overshoot read.
    assert "gex_state" not in snap["moc"] and "nearest_wall" not in snap["moc"]


def test_snapshot_gex_block_full_in_legacy_mode() -> None:
    snap = _snapshot_for(True)
    gex_block = snap["gex"]
    for key in (
        "regime", "zero_gamma", "total_net_gex", "nearest_wall_dist_pts",
        "call_walls", "put_walls",
        "dist_to_overhead_magnet_pts", "dist_to_support_below_pts",
    ):
        assert key in gex_block, key
    assert gex_block["zero_gamma"] == 7442.0
    assert _has_gex_level(lv["label"] for lv in snap["key_levels"])
    assert snap["moc"]["gex_state"] == "PIN"


# --------------------------------------------------------------------------- #
# System-prompt assembly — two stable variants
# --------------------------------------------------------------------------- #

def test_legacy_prompt_keeps_the_gex_machinery() -> None:
    legacy = _assemble_system_prompt(True)
    assert "THE GEX MAP IS YOUR PRIMARY FRAME" in legacy
    assert "ROOM RULE" in legacy
    assert "GEX CONTEXT" in legacy
    assert "GAMMA SIGN IS A ROUTER LABEL" not in legacy


def test_decoupled_prompt_is_sign_router_only() -> None:
    decoupled = _assemble_system_prompt(False)
    assert "GAMMA SIGN IS A ROUTER LABEL" in decoupled
    for gone in (
        "THE GEX MAP IS YOUR PRIMARY FRAME",
        "ROOM RULE",
        "GEX CONTEXT",
        "dist_to_overhead_magnet_pts",
        "gex_map",
    ):
        assert gone not in decoupled, gone
    # Shared methodology still present in both variants.
    for kept in ("OPTIONS FLOW & THE VANNA RALLY", "EMA IGNITION", "DISPLACEMENT",
                 "MARKET-ON-CLOSE", "YOUR JOB"):
        assert kept in decoupled, kept


def test_prompt_variants_share_the_closing_contract() -> None:
    for variant in (_assemble_system_prompt(True), _assemble_system_prompt(False)):
        assert variant.endswith("Respond ONLY with the JSON object the schema defines. No preamble.")


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
