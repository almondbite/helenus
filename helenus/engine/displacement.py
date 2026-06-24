"""
Displacement — the footprint of one-sided institutional ("smart money") flow.

A displacement is a sudden, aggressive thrust: large, full-bodied candles with
very small wicks, driven by massive one-sided volume. A big candle alone isn't a
signal, though — to be tradeable the move must clear three structural pillars:

  1. Liquidity sweep (the trap): the move starts right after price pierces a key
     level / prior swing just enough to trip a cluster of stops, then reverses.
  2. Market structure shift (MSS): the thrust closes decisively past a recent
     swing high/low — proof the trend actually changed, not just a wick.
  3. Fair value gap (FVG): because one side of the book can't keep up, the move
     leaves a 3-candle imbalance — the wicks of the 1st and 3rd candle don't
     overlap, leaving a gap price tends to revisit (a retrace target).

Engineering choice (see plan): the **displacement candle + FVG + MSS** are the
hard gate — all three are crisply computable from the OHLCV tape. The **liquidity
sweep** is fuzzier (over-requiring it suppresses valid setups), so it is detected
and surfaced as a strong conviction *booster*; flip `CONFIG.displacement.
require_sweep` to harden it into the gate once its parameters are tuned.

Pure and stateless — everything it needs is in the last ~N bars of MarketState.
This is Claude's input, not its job: the gate emits a candidate, Claude judges.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from helenus.config import CONFIG, DisplacementConfig
from helenus.engine.charm import CharmProfile
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Direction, MarketState


@dataclass(frozen=True)
class DisplacementReading:
    """One bar's displacement read: the thrust candle, the structure it broke, and
    the imbalance it left. `active` = candle + FVG + MSS (+ sweep if required)."""
    direction: Direction | None = None
    detected: bool = False              # a qualifying displacement candle exists
    body_pts: float = 0.0               # signed body of the displacement candle
    body_frac: float = 0.0              # body / full range (small wicks → near 1)
    vol_ratio: float = 0.0              # displacement volume vs the 20-bar baseline
    fvg: bool = False
    fvg_low: float | None = None        # imbalance zone (a retrace target)
    fvg_high: float | None = None
    mss: bool = False
    mss_level: float | None = None      # the swing the thrust closed past
    swept: bool = False                 # booster — stop-hunt pierce before the move
    swept_level: float | None = None
    active: bool = False
    note: str = ""


def _empty() -> DisplacementReading:
    return DisplacementReading()


def build_displacement(
    state: MarketState,
    gex: GexProfile | None = None,
    charm: CharmProfile | None = None,
    cfg: DisplacementConfig | None = None,
) -> DisplacementReading:
    """Evaluate the last three bars as an FVG triple — `c1` (bars[-3]), the
    displacement candle `c2` (bars[-2]), and `c3` (bars[-1]) — against the
    displacement / FVG / MSS pillars. Returns a `DisplacementReading`."""
    cfg = cfg or CONFIG.displacement
    bars = list(state.bars)
    if len(bars) < 3:
        return _empty()

    c1, c2, c3 = bars[-3], bars[-2], bars[-1]
    atr = state.atr()
    baseline = state.volume_baseline()
    if not (np.isfinite(atr) and atr > 0):
        return _empty()

    # --- The displacement candle (c2) ------------------------------------ #
    body = c2.close - c2.open
    abody = abs(body)
    rng = c2.high - c2.low
    body_frac = (abody / rng) if rng > 0 else 0.0
    vol_ratio = (c2.volume / baseline) if (np.isfinite(baseline) and baseline > 0) else 0.0
    direction = Direction.BULLISH if body > 0 else Direction.BEARISH
    candle_ok = (
        abody >= cfg.body_atr_mult * atr
        and body_frac >= cfg.body_frac
        and vol_ratio >= cfg.vol_ratio
    )

    # --- Fair value gap (c1 / c3 wicks don't overlap) -------------------- #
    if direction is Direction.BULLISH:
        gap = c3.low - c1.high
        fvg = gap >= cfg.min_fvg_pts
        fvg_low, fvg_high = (c1.high, c3.low) if fvg else (None, None)
    else:
        gap = c1.low - c3.high
        fvg = gap >= cfg.min_fvg_pts
        fvg_low, fvg_high = (c3.high, c1.low) if fvg else (None, None)

    # --- Market structure shift (close past a recent swing) -------------- #
    # The swing is the structure formed BEFORE the thrust: the mss_lookback bars
    # ending at c1 (exclude c2/c3, which are the move itself).
    struct = bars[-(cfg.mss_lookback + 2):-2]
    if struct:
        swing_high = max(b.high for b in struct)
        swing_low = min(b.low for b in struct)
        if direction is Direction.BULLISH:
            mss = c2.close > swing_high
            mss_level = swing_high
        else:
            mss = c2.close < swing_low
            mss_level = swing_low
    else:
        mss, mss_level = False, None

    # --- Liquidity sweep (booster): a stop-hunt before the move ---------- #
    swept, swept_level = _detect_sweep(bars, direction, c2, cfg)

    active = candle_ok and fvg and mss and (swept or not cfg.require_sweep)

    note = _note(direction, body, body_frac, vol_ratio, fvg, fvg_low, fvg_high,
                 mss, mss_level, swept, swept_level, candle_ok, active)

    return DisplacementReading(
        direction=direction if candle_ok else None,
        detected=candle_ok,
        body_pts=round(body, 2),
        body_frac=round(body_frac, 3),
        vol_ratio=round(vol_ratio, 2),
        fvg=fvg,
        fvg_low=round(fvg_low, 2) if fvg_low is not None else None,
        fvg_high=round(fvg_high, 2) if fvg_high is not None else None,
        mss=mss,
        mss_level=round(mss_level, 2) if mss_level is not None else None,
        swept=swept,
        swept_level=round(swept_level, 2) if swept_level is not None else None,
        active=active,
        note=note,
    )


def _detect_sweep(bars, direction, c2, cfg: DisplacementConfig):
    """A stop-hunt just before the thrust: in the few bars leading into c2, price
    took out a prior swing extreme (pierced beyond it) and the thrust then closed
    back through it. Uses a structure window EARLIER than the sweep window so the
    swept level isn't the sweep low itself."""
    need = cfg.mss_lookback + cfg.sweep_lookback + 2
    if len(bars) < need:
        return False, None
    sweep_win = bars[-(cfg.sweep_lookback + 2):-1]          # incl c1/c2, excl c3
    struct = bars[-need:-(cfg.sweep_lookback + 2)]          # the prior structure
    if not sweep_win or not struct:
        return False, None
    if direction is Direction.BULLISH:
        swing_low = min(b.low for b in struct)
        pierced = min(b.low for b in sweep_win) <= swing_low - cfg.sweep_pierce_pts
        reclaimed = c2.close > swing_low
        return (pierced and reclaimed), (swing_low if (pierced and reclaimed) else None)
    swing_high = max(b.high for b in struct)
    pierced = max(b.high for b in sweep_win) >= swing_high + cfg.sweep_pierce_pts
    reclaimed = c2.close < swing_high
    return (pierced and reclaimed), (swing_high if (pierced and reclaimed) else None)


def _note(direction, body, body_frac, vol_ratio, fvg, fvg_low, fvg_high,
          mss, mss_level, swept, swept_level, candle_ok, active) -> str:
    if not candle_ok:
        return "no qualifying displacement candle"
    d = direction.value
    parts = [
        f"{d} displacement",
        f"body {abs(body):.1f}pt ({body_frac:.0%} fill) {vol_ratio:.1f}x vol",
    ]
    parts.append(
        f"FVG {fvg_low:.0f}-{fvg_high:.0f}" if fvg and fvg_low is not None else "no FVG"
    )
    parts.append(
        f"MSS broke {mss_level:.0f}" if mss and mss_level is not None else "no MSS"
    )
    if swept and swept_level is not None:
        parts.append(f"swept {swept_level:.0f}")
    parts.append("ACTIVE" if active else "incomplete")
    return " | ".join(parts)
