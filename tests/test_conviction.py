"""
Offline tests for engine/conviction.py — the ported displacement-conviction
oscillator (Task 4: math-only port; SPY-proxy volume + ES cum-delta rewiring;
explicit degrades; liqRej separately exposed; enrich-only except LIQ_REJECT).

No network, no Schwab key. Runs under pytest *or* standalone:

    pytest tests/test_conviction.py
    python tests/test_conviction.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd

from helenus.config import CONFIG
from helenus.engine.conviction import (
    ConvictionEngine,
    Q_CHOP,
    Q_STREAK,
    W_CLOSE,
    W_DELTA,
    W_LIQ,
    W_VOL,
    W_VWAP,
    _Rma,
    _Rsi,
    _eff_pts,
    _norm,
    _streak_pts,
)
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Bar, Direction, MarketState, TriggerType, detect_candidate

CFG = CONFIG.conviction
DISABLED = dataclasses.replace(CFG, enabled=False)

_T0 = dt.datetime(2026, 6, 24, 10, 0)


@contextmanager
def conviction_mode(enabled: bool):
    old = CONFIG.conviction
    object.__setattr__(
        CONFIG, "conviction", dataclasses.replace(old, enabled=enabled)
    )
    try:
        yield
    finally:
        object.__setattr__(CONFIG, "conviction", old)


def _bar(i: int, c: float, *, o=None, h=None, l=None, v=100.0) -> Bar:
    return Bar(
        ts=_T0 + dt.timedelta(minutes=i),
        open=float(c if o is None else o),
        high=float(c if h is None else h),
        low=float(c if l is None else l),
        close=float(c),
        volume=v,
    )


def _calm(n: int, base: float = 100.0, start: int = 0) -> list[Bar]:
    """Gently oscillating tape: body 0.4, range 1.0 — warm, non-degenerate
    baselines (avgBody 0.4, ATR → 1.0)."""
    out = []
    for i in range(n):
        up = i % 2 == 0
        o = base - 0.2 if up else base + 0.2
        c = base + 0.2 if up else base - 0.2
        out.append(_bar(start + i, c, o=o, h=base + 0.5, l=base - 0.5))
    return out


def _run(eng: ConvictionEngine, bars: list[Bar], cum_deltas: list[float | None] | None = None):
    st = MarketState()
    readings = []
    for i, b in enumerate(bars):
        st.push_bar(b)
        cd = cum_deltas[i] if cum_deltas is not None else None
        readings.append(eng.on_bar(st, cd))
    return st, readings


# --------------------------------------------------------------------------- #
# Pure helpers — the Pine formula constants
# --------------------------------------------------------------------------- #

def test_norm_saturates_at_the_threshold() -> None:
    assert _norm(0.6, 1.2) == 0.5
    assert _norm(1.2, 1.2) == 1.0
    assert _norm(5.0, 1.2) == 1.0
    assert _norm(1.0, 0.0) == 0.0


def test_streak_points_peak_at_two_and_penalize_four() -> None:
    assert _streak_pts(0) == 0.0
    assert _streak_pts(1) == Q_STREAK * 0.5 == 6.0
    assert _streak_pts(2) == Q_STREAK == 12.0
    assert abs(_streak_pts(3) - 4.2) < 1e-9
    assert _streak_pts(4) == -Q_CHOP * 0.5 == -3.5
    assert _streak_pts(9) == -3.5


def test_efficiency_bands_including_the_chop_penalty() -> None:
    assert _eff_pts(0.75) == 8.0
    assert _eff_pts(0.55) == 4.8
    assert _eff_pts(0.40) == 2.4
    assert _eff_pts(0.30) == 0.0          # dead band between 0.22 and 0.35
    assert _eff_pts(0.10) == -Q_CHOP == -7.0


def test_rma_is_sma_seeded_wilder() -> None:
    r = _Rma(3)
    r.update(3.0)
    r.update(6.0)
    assert r.update(9.0) == 6.0           # SMA seed of the first 3
    assert abs(r.update(10.0) - (6.0 + (10.0 - 6.0) / 3.0)) < 1e-9


def test_rsi_rises_on_consistent_gains() -> None:
    rsi = _Rsi(14)
    v = 0.0
    for i in range(30):
        v = rsi.update(100.0 + i)
    assert v > 70.0


# --------------------------------------------------------------------------- #
# Component-level fixtures (via the exposed components dict)
# --------------------------------------------------------------------------- #

def _big_bull(i: int, base: float = 100.0, v: float = 500.0) -> Bar:
    """A full-bodied bull displacement candle off the calm tape: body 5,
    range 5.5, close at 96% of range."""
    return _bar(i, base + 5.0, o=base, h=base + 5.2, l=base - 0.3, v=v)


def test_close_position_and_vwap_components() -> None:
    eng = ConvictionEngine(CFG)
    bars = _calm(30) + [_big_bull(30)]
    _, readings = _run(eng, bars)
    comps = readings[-1].components["bull"]
    assert comps["close"] == W_CLOSE                     # closePos ≥ 0.58
    assert comps["vwap"] == W_VWAP                       # closed above session VWAP
    assert readings[-1].components["bear"]["close"] == 0.0


def test_volume_component_uses_the_proxy_and_saturates() -> None:
    eng = ConvictionEngine(CFG)
    bars = _calm(30) + [_big_bull(30, v=500.0)]          # relVol ≈ 3.9 ≥ 1.3
    _, readings = _run(eng, bars)
    assert readings[-1].components["bull"]["vol"] == W_VOL


def test_volume_fallback_when_baseline_cold() -> None:
    # Pine's own useVol=false fallback: 0.6 × weight, flagged degraded.
    eng = ConvictionEngine(CFG)
    _, readings = _run(eng, _calm(5))                    # < length priors
    r = readings[-1]
    assert r.components["bull"]["vol"] == W_VOL * 0.6
    assert any("volume_baseline" in d for d in r.degraded)


def test_delta_component_from_es_cum_delta() -> None:
    # Rising cumulative delta with an accelerating last interval → bull flow.
    eng = ConvictionEngine(CFG)
    bars = _calm(30) + [_big_bull(30)]
    cds: list[float | None] = [i * 1000.0 for i in range(30)] + [32_000.0]  # last diff 3000 > MA
    _, readings = _run(eng, bars, cds)
    r = readings[-1]
    assert r.components["bull"]["delta"] == W_DELTA
    assert r.components["bear"]["delta"] == 0.0
    assert not any("es_delta" in d for d in r.degraded)
    assert r.net_flow is not None


def test_delta_degrades_explicitly_when_stream_off() -> None:
    eng = ConvictionEngine(CFG)
    _, readings = _run(eng, _calm(30) + [_big_bull(30)])  # cum_delta always None
    r = readings[-1]
    assert r.components["bull"]["delta"] == 0.0
    assert any("es_delta" in d for d in r.degraded)
    assert r.net_flow is None
    # The static degrades are always declared.
    assert any("hv_rank" in d for d in r.degraded)
    assert any("prior_day_levels" in d for d in r.degraded)


# --------------------------------------------------------------------------- #
# liqRej — the sweep-reject reversal (the personal-edge piece)
# --------------------------------------------------------------------------- #

def test_liq_rej_bull_sweep_and_close_back_inside() -> None:
    eng = ConvictionEngine(CFG)
    bars = _calm(30)
    # Sweep the prior 14-bar low (99.5) and close green well back inside.
    bars.append(_bar(30, 101.0, o=100.0, h=101.2, l=98.0))
    _, readings = _run(eng, bars)
    r = readings[-1]
    assert r.liq_rej_bull and not r.liq_rej_bear
    assert r.swept_low == 99.5
    assert r.components["bull"]["liq"] == W_LIQ


def test_liq_rej_bear_is_the_mirror() -> None:
    eng = ConvictionEngine(CFG)
    bars = _calm(30)
    bars.append(_bar(30, 99.0, o=100.0, h=102.0, l=98.8))  # sweeps 100.5 high, closes red
    _, readings = _run(eng, bars)
    r = readings[-1]
    assert r.liq_rej_bear and not r.liq_rej_bull
    assert r.swept_high == 100.5


def test_bare_sweep_scores_partial_liq_only() -> None:
    # Sweeps the low but closes RED → no bull reject; the bull side still gets
    # the 0.4× "a sweep happened" credit.
    eng = ConvictionEngine(CFG)
    bars = _calm(30)
    bars.append(_bar(30, 99.2, o=100.0, h=100.1, l=98.0))
    _, readings = _run(eng, bars)
    r = readings[-1]
    assert not r.liq_rej_bull
    assert r.components["bull"]["liq"] == W_LIQ * 0.4


def test_no_sweep_no_liq_credit() -> None:
    eng = ConvictionEngine(CFG)
    _, readings = _run(eng, _calm(30))
    assert readings[-1].components["bull"]["liq"] == 0.0


# --------------------------------------------------------------------------- #
# Fire gate: raw candle + score + cooldown + MTF block
# --------------------------------------------------------------------------- #

def _pivot_calm(n: int, base: float = 100.0) -> list[Bar]:
    """Calm tape that leaves a fast pivot high just above base, so a breakout
    candle satisfies the structure-break leg of the raw gate."""
    bars = _calm(n, base)
    # Carve a 1-bar pivot high at base+1 three bars from the end.
    bars[-3] = _bar(n - 3, base + 0.2, o=base - 0.2, h=base + 1.0, l=base - 0.5)
    return bars


def test_fire_and_cooldown() -> None:
    eng = ConvictionEngine(CFG)
    bars = _pivot_calm(30)
    bars.append(_big_bull(30))                            # breaks the 101 pivot
    st, readings = _run(eng, bars)
    r1 = readings[-1]
    assert r1.score_bull >= CFG.fire_line
    assert r1.fire_bull
    # A second qualifying candle 2 bars later is inside the 4-bar cooldown.
    st.push_bar(_bar(31, 105.4, o=105.0, h=105.6, l=104.9))
    assert eng.on_bar(st, None) is not None
    st.push_bar(_bar(32, 112.0, o=105.4, h=112.3, l=105.2, v=800.0))
    r2 = eng.on_bar(st, None)
    assert r2.score_bull >= CFG.fire_line                 # conditions hold…
    assert not r2.fire_bull                               # …but the cooldown gates it


def test_mtf_block_suppresses_counter_trend_fire() -> None:
    # 60 declining bars mature the 1m EMA ladder bearish (bias −1); with the
    # block threshold pinned to 1, a bull fire is suppressed; with the filter
    # off, it fires.
    def tape() -> list[Bar]:
        bars = [
            _bar(i, 200.0 - i * 0.5, o=200.0 - i * 0.5 + 0.3,
                 h=200.0 - i * 0.5 + 0.6, l=200.0 - i * 0.5 - 0.3)
            for i in range(60)
        ]
        base = 200.0 - 59 * 0.5
        bars[-3] = dataclasses.replace(bars[-3], high=base + 1.0)   # pivot to break
        bars.append(_bar(60, base + 8.0, o=base, h=base + 8.4, l=base - 0.4, v=900.0))
        return bars

    blocked_cfg = dataclasses.replace(CFG, mtf_block_sum=1)
    eng = ConvictionEngine(blocked_cfg)
    _, readings = _run(eng, tape())
    r = readings[-1]
    assert r.mtf_sum <= -1
    assert r.score_bull >= CFG.fire_line and not r.fire_bull

    free_cfg = dataclasses.replace(CFG, mtf_block_sum=1, mtf_filter=False)
    eng2 = ConvictionEngine(free_cfg)
    _, readings2 = _run(eng2, tape())
    assert readings2[-1].fire_bull


def test_squeeze_fires_on_the_breakout_bar() -> None:
    # Dead-flat closes (BB width 0) with real ranges = squeeze ON (−3);
    # the breakout bar releases it: squeeze FIRED (+9).
    eng = ConvictionEngine(CFG)
    flat = [_bar(i, 100.0, h=100.5, l=99.5) for i in range(25)]
    _, readings = _run(eng, flat)
    assert readings[-1].components["bull"]["squeeze"] == -3.0
    st = MarketState()
    for b in flat:
        st.push_bar(b)
    st.push_bar(_big_bull(25))
    r = eng.on_bar(st, None)
    assert r.components["bull"]["squeeze"] == 9.0


def test_streak_component_peaks_at_two() -> None:
    eng = ConvictionEngine(CFG)
    bars = _calm(30)
    bars.append(_bar(30, 103.0, o=100.0, h=103.2, l=99.9))   # displacement candle 1
    bars.append(_bar(31, 106.5, o=103.0, h=106.7, l=102.9))  # displacement candle 2
    _, readings = _run(eng, bars)
    assert readings[-2].components["bull"]["streak"] == 6.0
    assert readings[-1].components["bull"]["streak"] == 12.0


# --------------------------------------------------------------------------- #
# Engine plumbing
# --------------------------------------------------------------------------- #

def test_disabled_returns_none() -> None:
    eng = ConvictionEngine(DISABLED)
    _, readings = _run(eng, _calm(5))
    assert all(r is None for r in readings)


def test_warm_seeds_without_firing() -> None:
    eng = ConvictionEngine(CFG)
    bars = _pivot_calm(30) + [_big_bull(30)]
    eng.warm(bars)                                        # the fire bar is in the backfill
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    assert eng.on_bar(st, None) is None                   # dedupe: already consumed
    st.push_bar(_bar(31, 105.2, o=105.0, h=105.4, l=104.8))
    r = eng.on_bar(st, None)
    assert r is not None and not r.fire_bull              # no replayed fire


def test_conv_signed_takes_the_winning_side() -> None:
    eng = ConvictionEngine(CFG)
    bars = _calm(30)
    bars.append(_bar(30, 95.0, o=100.0, h=100.2, l=94.8))  # hard bear candle
    _, readings = _run(eng, bars)
    r = readings[-1]
    assert r.score_bear > r.score_bull
    assert r.conv_signed == -r.score_bear


# --------------------------------------------------------------------------- #
# Gate integration — LIQ_REJECT + the final stack order
# --------------------------------------------------------------------------- #

def _gex(spot: float) -> GexProfile:
    return GexProfile(
        spot=spot, total_net_gex=0.0, zero_gamma=7600.0,
        call_walls=[], put_walls=[], by_strike=pd.DataFrame(),
    )


def _state2() -> MarketState:
    st = MarketState()
    st.push_bar(_bar(0, 7445.0))
    st.push_bar(_bar(1, 7446.0))
    return st


def _conv_stub(liq_bull=False, liq_bear=False, score=70.0):
    return SimpleNamespace(
        liq_rej_bull=liq_bull, liq_rej_bear=liq_bear,
        score_bull=score if liq_bull else 0.0,
        score_bear=score if liq_bear else 0.0,
        swept_low=7440.0, swept_high=7450.0, note="conv +70 (A)",
    )


def _disp_stub():
    return SimpleNamespace(active=True, mss_level=None, midpoint=None, note="disp")


def _scalp_stub():
    return SimpleNamespace(active=True, target_level=None, note="ema ignition")


def _vanna_stub():
    return SimpleNamespace(
        active=True, bearish_active=False,
        inflection_active=False, inflection_bearish=False, note="vanna",
    )


def test_liq_reject_fires_and_carries_the_swept_level() -> None:
    cand = detect_candidate(_state2(), _gex(7446.0), conviction=_conv_stub(liq_bull=True))
    assert cand is not None and cand.trigger is TriggerType.LIQ_REJECT
    assert cand.level is not None and cand.level.price == 7440.0
    assert "Swept the prior low" in cand.reason


def test_liq_reject_needs_the_fire_line() -> None:
    weak = _conv_stub(liq_bull=True, score=40.0)          # below fire_line 60
    assert detect_candidate(_state2(), _gex(7446.0), conviction=weak) is None


def test_final_stack_order() -> None:
    st, gex = _state2(), _gex(7446.0)
    # Displacement leads the family…
    cand = detect_candidate(
        st, gex, vanna=_vanna_stub(), scalp=_scalp_stub(),
        displacement=_disp_stub(), conviction=_conv_stub(liq_bull=True),
    )
    assert cand.trigger is TriggerType.DISPLACEMENT
    # …liq-reject beats the scalp and flow…
    cand = detect_candidate(
        st, gex, vanna=_vanna_stub(), scalp=_scalp_stub(),
        conviction=_conv_stub(liq_bull=True),
    )
    assert cand.trigger is TriggerType.LIQ_REJECT
    # …and the scalp still beats flow.
    cand = detect_candidate(st, gex, vanna=_vanna_stub(), scalp=_scalp_stub())
    assert cand.trigger is TriggerType.EMA_IGNITION


def test_conviction_disabled_restores_task2_order() -> None:
    with conviction_mode(False):
        cand = detect_candidate(
            _state2(), _gex(7446.0), vanna=_vanna_stub(), scalp=_scalp_stub(),
            displacement=_disp_stub(), conviction=_conv_stub(liq_bull=True),
        )
    # Task-2 order: CD → scalp → displacement → flow (no LIQ_REJECT).
    assert cand.trigger is TriggerType.EMA_IGNITION


# --------------------------------------------------------------------------- #
# Prompt + snapshot surfaces
# --------------------------------------------------------------------------- #

def test_prompt_carries_dispx_only_when_enabled() -> None:
    from helenus.engine.analyst import _assemble_system_prompt
    with_dispx = _assemble_system_prompt(False, True, True, True)
    assert "DISPLACEMENT CONVICTION + ZONES" in with_dispx
    assert "Liquidity Sweep Reject" in with_dispx
    without = _assemble_system_prompt(False, True, True, False)
    assert "DISPLACEMENT CONVICTION + ZONES" not in without
    legacy = _assemble_system_prompt(True, False, False, False)   # the original
    assert "DISPLACEMENT CONVICTION" not in legacy


def test_snapshot_blocks_shape() -> None:
    from helenus.engine.analyst import _conviction_block, _zones_block
    eng = ConvictionEngine(CFG)
    _, readings = _run(eng, _calm(30) + [_big_bull(30)])
    block = _conviction_block(readings[-1])
    assert block["available"] is True
    assert "conv_signed" in block and "tier" in block
    assert "components" in block and "degraded" in block
    assert "raw_dir" not in block["components"]
    assert _conviction_block(None) == {"available": False}
    assert _zones_block(None) == {"available": False}


# --------------------------------------------------------------------------- #
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
