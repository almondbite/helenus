"""
Offline tests for engine/scalp.py — the 1m 5/9 EMA-ignition contract scalp.

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_scalp.py
    python tests/test_scalp.py

The EMA recursion is checked against pandas `ewm(adjust=False)`; everything else
asserts the structural contract the trade logic relies on — the cross/reclaim
trigger, the gate stack (regime / vanna headwind / SPX confirm / room / chop /
dual-bleed / spread), contract selection, and the level→premium translation.
"""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from helenus.config import CONFIG
from helenus.data.schwab_feed import ET
from helenus.engine.flow import VannaReading
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Bar, Direction, MarketState
from helenus.engine.scalp import (
    ContractQuote,
    EmaTracker,
    ScalpEngine,
    _Ema,
    _bleeding,
    extract_contracts,
    premium_target,
    select_target_contract,
    vanna_headwind,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

_T0 = dt.datetime(2026, 6, 22, 13, 0, tzinfo=ET)

# Decline (top 5012) then a rising run; the 5/9 cross lands on the FINAL bar at
# close 5001 — session high stays 11pt away and the nearest round number (5010)
# sits 9pt above, so the room gate (≥4pt) passes.
_CROSS_UP_CLOSES = list(range(5012, 4994, -1)) + list(range(4996, 5002))


def _bars(closes, *, heavy_tail: int = 0) -> list[Bar]:
    """Bars 1 minute apart, no wick (high=low=close). When `heavy_tail` > 0 the
    last N bars carry heavy volume so session VWAP is pulled below the final
    close (a confirmed reclaim) rather than sitting above it."""
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        vol = 8000.0 if (heavy_tail and i >= n - heavy_tail) else 50.0
        out.append(
            Bar(ts=_T0 + dt.timedelta(minutes=i), open=float(c), high=float(c),
                low=float(c), close=float(c), volume=vol)
        )
    return out


def _green_gex(spot: float = 5001.0) -> GexProfile:
    """Negative-gamma (momentum-friendly) structure with walls far from spot."""
    return GexProfile(
        spot=spot, total_net_gex=-5e8, zero_gamma=5050.0,
        call_walls=[(5030.0, -1.0)], put_walls=[(4970.0, 1.0)],
        by_strike=pd.DataFrame(),
    )


def _red_gex(spot: float = 5001.0) -> GexProfile:
    """High-positive-gamma (mean-reverting) structure — momentum stand-down."""
    return GexProfile(
        spot=spot, total_net_gex=5e8, zero_gamma=4950.0,
        call_walls=[(5030.0, 1.0)], put_walls=[(4970.0, -1.0)],
        by_strike=pd.DataFrame(),
    )


def _vanna(vix_change: float) -> VannaReading:
    return VannaReading(
        vix_change=vix_change, vix_falling=vix_change < 0, otm_call_flow=0.0,
        otm_put_flow=0.0, call_flow_dominance=0.0, active=False,
        bearish_active=False, label="x", note="x",
    )


def _chain(spot: float = 5001.0) -> dict:
    """Minimal $SPXW-shaped chain with a ~0.35-delta OTM call and put, tight spread."""
    def c(strike, delta):
        return {"strikePrice": strike, "mark": 3.0, "bid": 2.9, "ask": 3.1,
                "delta": delta, "gamma": 0.02, "totalVolume": 500}
    return {
        "underlying": {"mark": spot},
        "callExpDateMap": {"k": {"5010.0": [c(5010, 0.35)], "5020.0": [c(5020, 0.20)]}},
        "putExpDateMap": {"k": {"4990.0": [c(4990, -0.35)], "4980.0": [c(4980, -0.20)]}},
    }


def _approx(a, b, tol=1e-6):
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


# --------------------------------------------------------------------------- #
# EMA math
# --------------------------------------------------------------------------- #

def test_ema_matches_pandas_ewm() -> None:
    xs = [100, 101, 102, 101, 103, 104, 103, 105, 106, 107, 106, 108]
    e = _Ema(5)
    got = [e.update(x) for x in xs]
    exp = pd.Series(xs).ewm(span=5, adjust=False).mean().tolist()
    assert all(_approx(a, b) for a, b in zip(got, exp))


def test_ema_maturity_flag() -> None:
    e = _Ema(5)
    for _ in range(4):
        e.update(100.0)
    assert not e.mature
    e.update(100.0)
    assert e.mature


# --------------------------------------------------------------------------- #
# Cross / reclaim trigger
# --------------------------------------------------------------------------- #

def test_cross_up_fires_on_the_final_bar() -> None:
    e = EmaTracker(5, 9, 200)
    fired = [i for i, c in enumerate(_CROSS_UP_CLOSES) if (e.update(c) or e.cross_up)]
    assert fired[-1] == len(_CROSS_UP_CLOSES) - 1
    assert e.bull_stack


def test_cross_dn_is_the_mirror() -> None:
    closes = [4990 + (5012 - c) for c in _CROSS_UP_CLOSES]  # vertically mirrored
    e = EmaTracker(5, 9, 200)
    fired = [i for i, c in enumerate(closes) if (e.update(c) or e.cross_dn)]
    assert fired[-1] == len(closes) - 1
    assert not e.bull_stack


def test_reclaim_off_a_local_bottom() -> None:
    # Rise to set 5>9, dip below the 5 EMA, then reclaim it on the final bar —
    # WITHOUT a fresh 5/9 cross (the higher-quality reclaim variant).
    closes = list(range(4990, 5012)) + [5006, 5009]
    st, eng = MarketState(), ScalpEngine()
    reading = None
    for b in _bars(closes):
        st.push_bar(b)
        reading = eng.on_bar(st, None, None, None)
    assert reading.cross_type == "reclaim"
    assert reading.direction is Direction.BULLISH


# --------------------------------------------------------------------------- #
# Contract selection + premium translation
# --------------------------------------------------------------------------- #

def test_select_picks_nearest_target_delta_on_the_right_side() -> None:
    contracts = extract_contracts(_chain())
    call = select_target_contract(contracts, 5001.0, Direction.BULLISH)
    put = select_target_contract(contracts, 5001.0, Direction.BEARISH)
    assert call is not None and call.side == "call" and call.strike == 5010
    assert put is not None and put.side == "put" and put.strike == 4990


def test_premium_target_translation_with_gamma() -> None:
    c = ContractQuote(strike=5010, side="call", premium=3.0, bid=2.9, ask=3.1,
                      delta=0.35, gamma=0.02, volume=500)
    # Target 9pt above spot: 3.0 + 0.35*9 + 0.5*0.02*81 = 6.96.
    assert _approx(premium_target(c, 5010.0, 5001.0), 6.96)
    # A call target ABOVE spot lifts the premium; the gamma term only adds to it.
    flat = 3.0 + 0.35 * 9
    assert premium_target(c, 5010.0, 5001.0) > flat


def test_spread_gate_rejects_wide_quotes() -> None:
    tight = ContractQuote(5010, "call", 3.0, 2.95, 3.05, 0.35, 0.02, 500)
    wide = ContractQuote(5010, "call", 3.0, 2.0, 4.0, 0.35, 0.02, 500)
    assert tight.spread_pct <= CONFIG.scalp.max_spread_pct
    assert wide.spread_pct > CONFIG.scalp.max_spread_pct


# --------------------------------------------------------------------------- #
# Vanna headwind (per-direction premium-vega read)
# --------------------------------------------------------------------------- #

def test_vanna_headwind_on_falling_iv() -> None:
    drop = -CONFIG.scalp.vix_headwind_pts - 0.1
    # Falling IV bleeds vega out of any long option — headwind either direction.
    assert vanna_headwind(Direction.BULLISH, drop)
    assert vanna_headwind(Direction.BEARISH, drop)


def test_vanna_no_headwind_on_stable_or_rising_iv() -> None:
    assert not vanna_headwind(Direction.BULLISH, 0.0)
    assert not vanna_headwind(Direction.BEARISH, +0.5)


# --------------------------------------------------------------------------- #
# Premium-series reads (dual-bleed + divergence)
# --------------------------------------------------------------------------- #

def test_bleeding_detects_lower_highs_and_lower_lows() -> None:
    assert _bleeding([10.0, 9.5, 9.0, 8.0, 7.0, 6.5])
    assert not _bleeding([6.0, 6.5, 7.0, 8.0, 9.0, 9.5])   # rising
    assert not _bleeding([8.0, 8.0])                       # too short


def test_premium_divergence_bullish() -> None:
    # Index makes a lower low while the ATM call premium makes a higher low.
    st, eng = MarketState(), ScalpEngine()
    for b in _bars(list(range(5010, 4994, -1))):           # index grinding lower
        st.push_bar(b)
    eng._atm_call.extend([2.0, 1.8, 1.6, 1.9, 2.1, 2.3])   # call carving a higher low
    assert eng._premium_divergence(st, Direction.BULLISH)


# --------------------------------------------------------------------------- #
# Full gate stack via on_bar
# --------------------------------------------------------------------------- #

def _run(closes, gex, vanna, *, heavy_tail=6, chain=None) -> "object":
    st, eng = MarketState(), ScalpEngine()
    if chain is not None:
        eng.on_chain(chain, chain["underlying"]["mark"])
    reading = None
    for b in _bars(closes, heavy_tail=heavy_tail):
        st.push_bar(b)
        reading = eng.on_bar(st, gex, vanna, None)
    return st, eng, reading


def test_active_when_all_gates_pass() -> None:
    st, eng, r = _run(_CROSS_UP_CLOSES, _green_gex(), _vanna(0.0), chain=_chain())
    assert r.direction is Direction.BULLISH and r.cross_type == "cross"
    assert r.regime_ok and r.vanna_ok and r.confirm_ok and r.room_ok
    assert r.chop_ok and r.bleed_ok and r.spread_ok
    assert r.active
    assert r.target_level is not None and r.room_to_level_pts >= CONFIG.scalp.min_room_pts
    assert r.target_contract is not None and r.premium_target is not None


def test_positive_gamma_is_slow_grind_not_blocked() -> None:
    # Gate 0 is no longer a hard block: high-positive GEX still fires, just flagged
    # `slow_grind` for the analyst to temper (rather than suppressed).
    _, _, r = _run(_CROSS_UP_CLOSES, _red_gex(), _vanna(0.0), chain=_chain())
    assert r.direction is Direction.BULLISH
    assert not r.regime_ok and r.slow_grind
    assert r.active


def test_gated_on_vanna_headwind() -> None:
    drop = -CONFIG.scalp.vix_headwind_pts - 0.1
    _, _, r = _run(_CROSS_UP_CLOSES, _green_gex(), _vanna(drop), chain=_chain())
    assert r.vanna_headwind and not r.vanna_ok
    assert not r.active


def test_gated_in_chop() -> None:
    # Seed three recent crosses so the chop counter trips on this bar's cross.
    st, eng = MarketState(), ScalpEngine()
    eng.on_chain(_chain(), 5001.0)
    bars = _bars(_CROSS_UP_CLOSES, heavy_tail=6)
    final_ts = bars[-1].ts
    for t in (final_ts - dt.timedelta(minutes=2),
              final_ts - dt.timedelta(minutes=1),
              final_ts - dt.timedelta(seconds=30)):
        eng.chop.record(t)
    r = None
    for b in bars:
        st.push_bar(b)
        r = eng.on_bar(st, _green_gex(), _vanna(0.0), None)
    assert r.in_chop and not r.chop_ok
    assert not r.active


def test_gated_on_insufficient_room() -> None:
    # Shift the whole path up so the final close lands at 5007 → the nearest round
    # number (5010) is only 3pt, below the lowered 4pt room floor.
    closes = [c + 6 for c in _CROSS_UP_CLOSES]   # final close 5007 → 5010 is 3pt
    _, _, r = _run(closes, _green_gex(spot=5007.0), _vanna(0.0),
                   chain=_chain(spot=5007.0))
    assert r.room_to_level_pts < CONFIG.scalp.min_room_pts
    assert not r.room_ok and not r.active


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

def test_no_bars_is_inactive() -> None:
    r = ScalpEngine().on_bar(MarketState(), None, None, None)
    assert not r.active and r.direction is None


def test_no_contract_blocks_active() -> None:
    # Green regime + clean cross but no chain loaded → no strike → not active.
    _, _, r = _run(_CROSS_UP_CLOSES, _green_gex(), _vanna(0.0), chain=None)
    assert r.target_contract is None
    assert not r.active


def test_warm_seeds_emas_without_reprocessing() -> None:
    bars = _bars(_CROSS_UP_CLOSES)
    eng = ScalpEngine()
    eng.warm(bars)
    assert eng.ema.fast.value is not None
    # A re-run of the last bar must be a no-op (dedupe on timestamp).
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    r = eng.on_bar(st, _green_gex(), _vanna(0.0), None)
    assert r.direction is None  # already consumed by warm()


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
