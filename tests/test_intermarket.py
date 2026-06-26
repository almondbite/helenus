"""
Offline tests for engine/intermarket.py — the intermarket-convergence layer.

No network. Runs under pytest *or* standalone:

    pytest tests/test_intermarket.py
    python tests/test_intermarket.py

GexProfiles are hand-built with just the fields `regime` reads (spot, total
net GEX, zero-gamma); macro quotes are minimal {symbol: {"quote": {...}}} dicts.
"""

from __future__ import annotations

import pandas as pd

from helenus.config import CONFIG
from helenus.engine.gex import GexProfile
from helenus.engine.intermarket import (
    ESTracker,
    _leg_direction,
    build_intermarket,
)


def _es(bid: float, ask: float):
    """An ESReading with a chosen resting bid/ask imbalance."""
    return ESTracker().update({"totalVolume": 0, "bidSize": bid, "askSize": ask})
from helenus.engine.scan2 import Direction

BULL, BEAR = Direction.BULLISH, Direction.BEARISH


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _profile(spot: float, net: float, zero: float) -> GexProfile:
    """Minimal GexProfile whose `regime` resolves as intended.
    POSITIVE_GAMMA needs spot>=zero AND net>=0; NEGATIVE_GAMMA the mirror."""
    return GexProfile(
        spot=spot,
        total_net_gex=net,
        zero_gamma=zero,
        call_walls=[],
        put_walls=[],
        by_strike=pd.DataFrame(),
    )


def _neg_profile(spot: float = 100.0) -> GexProfile:
    # spot below flip + negative net -> NEGATIVE_GAMMA
    return _profile(spot, net=-50_000, zero=spot + 5)


def _pos_profile(spot: float = 100.0) -> GexProfile:
    # spot above flip + positive net -> POSITIVE_GAMMA
    return _profile(spot, net=50_000, zero=spot - 5)


def _quotes(**pct_by_symbol: float) -> dict:
    """{'QQQ': {'quote': {'netPercentChange': x, 'lastPrice': 1.0}}, ...}"""
    return {
        sym: {"quote": {"netPercentChange": pct, "lastPrice": 1.0}}
        for sym, pct in pct_by_symbol.items()
    }


# --------------------------------------------------------------------------- #
# _leg_direction
# --------------------------------------------------------------------------- #

def test_leg_direction_deadband() -> None:
    db = CONFIG.intermarket.pct_deadband
    assert _leg_direction(0.50, db) is BULL
    assert _leg_direction(-0.50, db) is BEAR
    assert _leg_direction(0.0, db) is None
    assert _leg_direction(db - 0.001, db) is None     # just inside the band
    assert _leg_direction(db, db) is BULL             # boundary counts


# --------------------------------------------------------------------------- #
# ESTracker
# --------------------------------------------------------------------------- #

def test_es_flow_is_interval_delta_and_clamps_on_reset() -> None:
    tr = ESTracker()
    r1 = tr.update({"totalVolume": 1000, "bidSize": 300, "askSize": 100})
    assert r1.volume_flow == 0.0                      # first read: no prior delta
    r2 = tr.update({"totalVolume": 1500, "bidSize": 300, "askSize": 100})
    assert r2.volume_flow == 500.0                    # 1500 - 1000
    r3 = tr.update({"totalVolume": 200, "bidSize": 300, "askSize": 100})
    assert r3.volume_flow == 0.0                      # daily reset clamps, not negative


def test_es_imbalance_sign() -> None:
    tr = ESTracker()
    bid_heavy = tr.update({"totalVolume": 0, "bidSize": 300, "askSize": 100})
    assert bid_heavy.imbalance > 0                    # (300-100)/400 = 0.5
    assert bid_heavy.direction is BULL                # clears es_imbalance_min
    tr2 = ESTracker()
    ask_heavy = tr2.update({"totalVolume": 0, "bidSize": 100, "askSize": 300})
    assert ask_heavy.imbalance < 0
    assert ask_heavy.direction is BEAR
    tr3 = ESTracker()
    balanced = tr3.update({"totalVolume": 0, "bidSize": 105, "askSize": 100})
    assert balanced.direction is None                 # below the threshold


# --------------------------------------------------------------------------- #
# Alignment + boost
# --------------------------------------------------------------------------- #

def test_aligned_full_boost() -> None:
    # signal BULLISH; QQQ + SPY both up; QQQ regime matches SPX (both NEGATIVE).
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=0.50, SPY=0.30),
    )
    assert im.alignment(BULL) == "ALIGNED"
    assert im.confidence_boost(BULL) == CONFIG.intermarket.align_boost_max


def test_partial_when_regime_disagrees() -> None:
    # QQQ + SPY confirm direction, but QQQ regime (NEG) != SPX regime (POS).
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_pos_profile(),
        macro_quotes=_quotes(QQQ=0.50, SPY=0.30),
    )
    assert im.alignment(BULL) == "PARTIAL"
    expected = CONFIG.intermarket.align_boost_max * CONFIG.intermarket.partial_frac
    assert im.confidence_boost(BULL) == expected


def test_partial_when_spy_does_not_confirm() -> None:
    # QQQ confirms + regime matches, but SPY is flat (no confirm) -> PARTIAL.
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=0.50, SPY=0.0),
    )
    assert im.alignment(BULL) == "PARTIAL"


def test_divergent_when_qqq_opposes() -> None:
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=-0.50, SPY=0.30),
    )
    assert im.alignment(BULL) == "DIVERGENT"
    assert im.confidence_boost(BULL) == CONFIG.intermarket.divergence_adj  # 0 default


def test_neutral_when_qqq_flat() -> None:
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=0.0, SPY=0.30),
    )
    assert im.alignment(BULL) == "NEUTRAL"
    assert im.confidence_boost(BULL) == 0.0


def test_conflicted_regime_base_token_matches() -> None:
    # SPX is NEGATIVE_GAMMA_CONFLICTED; QQQ is plain NEGATIVE_GAMMA — base tokens
    # match, so regime agreement holds and a confirming QQQ+SPY grades ALIGNED.
    spx = _profile(spot=100.0, net=-25_000, zero=95.0)  # spot>flip but net<0
    assert spx.regime == "NEGATIVE_GAMMA_CONFLICTED"
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=spx,
        macro_quotes=_quotes(QQQ=0.50, SPY=0.30),
    )
    assert im.alignment(BULL) == "ALIGNED"


def test_split_legs_grade_divergent() -> None:
    # QQQ confirms the bull + regime matches, but SPY actively leans bearish.
    # One green leg must not validate a setup the other opposes -> DIVERGENT.
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=0.50, SPY=-0.30),
    )
    assert im.alignment(BULL) == "DIVERGENT"
    assert im.confidence_boost(BULL) == CONFIG.intermarket.divergence_adj


# --------------------------------------------------------------------------- #
# ES opposition penalty — a demotion (not a veto) for opposing /ES imbalance
# --------------------------------------------------------------------------- #

def test_es_penalty_demotes_opposing_long() -> None:
    # Ask-heavy /ES (-0.5) opposes a LONG beyond es_oppose_min -> fixed penalty.
    im = build_intermarket(
        es_reading=_es(bid=100, ask=300),     # imbalance -0.5
        spy_profile=None, qqq_profile=None, spx_profile=_neg_profile(),
        macro_quotes={},
    )
    assert im.es_opposition_penalty(BULL) == -CONFIG.intermarket.es_oppose_penalty


def test_es_penalty_demotes_opposing_short() -> None:
    # Bid-heavy /ES (+0.5) opposes a SHORT beyond es_oppose_min -> fixed penalty.
    im = build_intermarket(
        es_reading=_es(bid=300, ask=100),     # imbalance +0.5
        spy_profile=None, qqq_profile=None, spx_profile=_neg_profile(),
        macro_quotes={},
    )
    assert im.es_opposition_penalty(BEAR) == -CONFIG.intermarket.es_oppose_penalty


def test_es_penalty_zero_when_aligned_or_small_or_absent() -> None:
    # Bid-heavy /ES CONFIRMS a long -> no penalty.
    aligned = build_intermarket(
        es_reading=_es(bid=300, ask=100), spy_profile=None, qqq_profile=None,
        spx_profile=_neg_profile(), macro_quotes={},
    )
    assert aligned.es_opposition_penalty(BULL) == 0.0
    # Opposing but BELOW the threshold (|imbalance| 0.2 < 0.4) -> no penalty.
    mild = build_intermarket(
        es_reading=_es(bid=100, ask=150), spy_profile=None, qqq_profile=None,
        spx_profile=_neg_profile(), macro_quotes={},
    )
    assert mild.es_opposition_penalty(BULL) == 0.0
    # No /ES read at all -> no penalty.
    none = build_intermarket(
        es_reading=None, spy_profile=None, qqq_profile=None,
        spx_profile=_neg_profile(), macro_quotes={},
    )
    assert none.es_opposition_penalty(BULL) == 0.0


def test_boost_clamps_at_95_at_call_site() -> None:
    # The analyst applies min(base + boost, 95); verify the arithmetic holds.
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=_neg_profile(),
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=0.50, SPY=0.30),
    )
    boost = im.confidence_boost(BULL)
    assert min(90.0 + boost, 95.0) == 95.0


def test_build_intermarket_degrades_to_quote_only() -> None:
    # No QQQ chain, but the macro quote still gives a directional lean. The leg
    # gets UNKNOWN regime -> can't grade ALIGNED, but direction still confirms.
    im = build_intermarket(
        es_reading=None,
        spy_profile=_neg_profile(),
        qqq_profile=None,
        spx_profile=_neg_profile(),
        macro_quotes=_quotes(QQQ=0.50, SPY=0.30),
    )
    assert im.qqq is not None
    assert im.qqq.regime == "UNKNOWN"
    assert im.qqq.direction is BULL
    assert im.alignment(BULL) == "PARTIAL"   # confirms direction, no regime confirm


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
