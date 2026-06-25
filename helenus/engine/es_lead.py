"""
ES-leads-SPX micro-lead — recover the staleness baked into the poll-based tape.

Schwab does not stream the $SPX cash index (indices are screener-only), so the
price tape is folded from ~15s $SPX poll samples: at every bar close the bar's
`close` is the last poll, up to ~15-30s stale. /ES (E-mini S&P futures) IS streamed
tick-by-tick and futures lead the cash index — so at bar close /ES already reflects
a move the SPX tape hasn't caught up to.

This module reads a genuine /ES momentum THRUST off the streamed /ES bar-close
series (the same dual gate scan2's range-expansion uses on SPX: an ATR multiple AND
a points floor). The bot turns a thrust into an ARM; scan2 then fires an ES-led
momentum candidate the moment the SPX bar starts confirming that direction — up to
one bar earlier than the SPX-only range-expansion would. ARM on /ES, confirm on SPX.

Only /ES *displacement* (differences) is ever used — never /ES vs SPX absolute
levels — so the futures basis is irrelevant and no scaling is needed.

Pure and deterministic — no I/O, no awaits. Fed one streamed /ES last price per bar
close by the bot, behind the stream's staleness gate; when the /ES stream is stale
or disabled it simply isn't fed and the poll-only path is unchanged.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from helenus.config import CONFIG, ESLeadConfig
from helenus.engine.scan2 import Direction


@dataclass(frozen=True)
class ESLeadReading:
    break_direction: Direction | None    # a qualified /ES thrust this lookback, or None
    displacement_pts: float              # net /ES move over the lookback (signed)
    atr_mult: float                      # |displacement| / ES-ATR (0 when ATR cold)
    note: str


class ESLeadTracker:
    """Folds streamed /ES bar-close prices into a rolling series and reads the
    leading momentum thrust off it. Mirrors MarketState.atr / scan2's range-
    expansion math, but on /ES — the instrument that leads the cash index."""

    def __init__(self, cfg: ESLeadConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.es_lead
        self._closes: deque[float] = deque(maxlen=max(self.cfg.es_history, self.cfg.lookback_bars + 2))
        self.updated: float = 0.0

    def observe(self, es_last: float, now: float | None = None) -> None:
        """Append the freshest streamed /ES last price (one sample per bar close).
        A non-finite/non-positive value is ignored so a missing tick can't poison
        the series."""
        self.updated = now if now is not None else time.monotonic()
        if es_last == es_last and es_last > 0:        # not NaN, positive
            self._closes.append(float(es_last))

    def _atr(self) -> float:
        """Mean bar-to-bar absolute move over the lookback window — a cheap /ES ATR.
        NaN until enough samples exist."""
        k = self.cfg.lookback_bars
        if len(self._closes) <= k:
            return float("nan")
        closes = list(self._closes)
        diffs = np.abs(np.diff(closes[-(k + 1):]))
        return float(np.mean(diffs)) if len(diffs) else float("nan")

    def reading(self) -> ESLeadReading:
        cfg = self.cfg
        k = cfg.lookback_bars
        if len(self._closes) <= k:
            return ESLeadReading(None, 0.0, 0.0, "ES lead: warming up")
        closes = list(self._closes)
        disp = closes[-1] - closes[-1 - k]
        atr = self._atr()
        atr_mult = abs(disp) / atr if (atr == atr and atr > 0) else 0.0

        clears_pts = abs(disp) >= cfg.min_pts
        clears_atr = atr == atr and atr > 0 and abs(disp) >= cfg.atr_mult * atr
        direction: Direction | None = None
        if clears_pts and clears_atr:
            direction = Direction.BULLISH if disp > 0 else Direction.BEARISH

        if direction is not None:
            note = (
                f"/ES {abs(disp):.1f}pt {'up' if disp > 0 else 'down'}-thrust over "
                f"{k} bars ({atr_mult:.1f}× ES-ATR) — futures leading"
            )
        else:
            note = f"/ES {disp:+.1f}pt over {k} bars; no qualified thrust"
        return ESLeadReading(
            break_direction=direction,
            displacement_pts=round(disp, 2),
            atr_mult=round(atr_mult, 2),
            note=note,
        )
