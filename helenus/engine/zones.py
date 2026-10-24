"""
Displacement zones — the MATH of the Pine "Displacement Zones [Volatility
Engine] v6", ported as pure Python. Formulas only; no Pine runtime (no boxes/
labels/dashboard, no request.security, no session filters).

What this is inside Helenus: PERSISTENT structure maintained across bars — the
upgrade from levels (single prices) to stateful ZONES with a lifecycle:

  spawn   a displacement candle that passes the raw gate AND the transparent
          8-component score (weights 18/18/12/12/10/12/10/8 = 100, threshold
          min_score) leaves a supply/demand zone (its body bounds),
  ACTIVE → TESTED   price trades back into the zone,
        → MITIGATED price tags the zone and closes back outside the near edge
          (the zone reacted — still valid),
        → BROKEN    price CLOSES through the far edge (invalidated),
  with a touch count and a half-life age decay on the zone's effective
  strength. FVGs get the same treatment: OPEN → CE (50% consequent
  encroachment) → FILLED.

The reading exposes the relation of price to the nearest zones (APPROACHING /
TESTING / MITIGATING / CLEAR) as queryable state for the approach-arm logic
and Claude's snapshot. Zone edges are deliberately NOT key levels — no scan2
trigger initiates at one; zones inform, they don't fire.

Deliberate deviations + degrades (see ZoneConfig): touch counting ports the
INTENT — one count per DISTINCT entry into the zone — rather than Pine's
first-touch-only quirk; structure break uses the fast (1-bar) pivots (the Pine
default mode); cVol pins to 0.5 while the SPY-proxy baseline is cold; cRegime
(VIX 252-day rank), cMtf (a 60m displacement baseline that can never warm
intraday), and cVrp (index − daily RV) pin to Pine's own 0.5 not-applicable
midpoint — all flagged in `ZoneReading.degraded`.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass, field

from helenus.config import CONFIG, ZoneConfig
from helenus.engine.scan2 import Bar, MarketState

# The Pine score weights (the formula, deliberately not config).
W_BODY = 18.0
W_ATR = 18.0
W_CLOSE = 12.0
W_STRUCT = 12.0
W_VOL = 10.0
W_REGIME = 12.0
W_MTF = 10.0
W_VRP = 8.0

# Zone / FVG lifecycle states.
ACTIVE = "ACTIVE"
TESTED = "TESTED"
MITIGATED = "MITIGATED"
BROKEN = "BROKEN"

FVG_OPEN = "OPEN"
FVG_CE = "CE"
FVG_FILLED = "FILLED"


def _norm_ratio(r: float) -> float:
    """Pine normRatio: ratio ≥1 means threshold met → 0..1 with r=1 → 0.5,
    r≥2 → 1.0."""
    return min(max(r, 0.0) / 2.0, 1.0)


def _convict(p: float, thresh: float) -> float:
    """Pine convict: close-position conviction → 0..1 (0.5 at the threshold,
    1.0 at the extreme)."""
    if p >= thresh:
        return 0.5 + 0.5 * min((p - thresh) / max(1.0 - thresh, 0.01), 1.0)
    return 0.5 * min(p / max(thresh, 0.01), 1.0)


@dataclass
class Zone:
    """A live supply/demand zone (mutable — the engine owns it)."""
    top: float
    bottom: float
    is_bull: bool
    score: float
    state: str = ACTIVE
    start_idx: int = 0
    touches: int = 0
    inside: bool = False          # was price inside on the previous bar (distinct-entry counting)
    reacted_this_bar: bool = False

    def effective(self, now_idx: int, decay_bars: int) -> float:
        """Age-decayed strength: score · 0.5^(age / decay_bars)."""
        age = max(now_idx - self.start_idx, 0)
        return self.score * math.pow(0.5, age / max(float(decay_bars), 1.0))


@dataclass
class Fvg:
    top: float
    bottom: float
    is_bull: bool
    state: str = FVG_OPEN
    start_idx: int = 0

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class ZoneView:
    """An immutable per-bar snapshot of one zone, for readings/snapshot blocks."""
    top: float
    bottom: float
    is_bull: bool
    score: float
    state: str
    touches: int
    effective: float
    age_bars: int


@dataclass(frozen=True)
class ZoneReading:
    zones: tuple[ZoneView, ...]              # live zones, oldest first
    fvgs: tuple[Fvg, ...]                    # tracked FVGs (frozen copies)
    spawned: ZoneView | None                 # the zone THIS bar created, if any
    # Relation of price to the nearest live zones — the approach-arm surface.
    relation: str                            # MITIGATING | TESTING | APPROACHING | CLEAR
    relation_zone: ZoneView | None
    nearest_bull_below: ZoneView | None      # nearest live bullish zone at/below price
    nearest_bear_above: ZoneView | None      # nearest live bearish zone at/above price
    n_active: int = 0
    n_tested: int = 0
    degraded: tuple[str, ...] = ()
    note: str = ""


class ZoneEngine:
    """Owns the persistent zone/FVG lists across bars. `on_bar(state)` once per
    closed tape bar → ZoneReading (None when disabled). `warm(bars)` replays the
    backfill so a mid-session restart rebuilds the same zones."""

    def __init__(self, cfg: ZoneConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.zones
        c = self.cfg
        self._bars: deque[Bar] = deque(maxlen=c.length + 5)
        self._atr_rma_value: float | None = None
        self._atr_seed: list[float] = []
        self.zones: list[Zone] = []
        self.fvgs: list[Fvg] = []
        self._last_ph: float | None = None
        self._last_pl: float | None = None
        self._i = -1
        self._last_bar_ts: dt.datetime | None = None

    # -- warmup ---------------------------------------------------------------- #

    def warm(self, bars: list[Bar]) -> None:
        for b in bars:
            self._step(b)
        if bars:
            self._last_bar_ts = bars[-1].ts

    # -- indicators ------------------------------------------------------------ #

    def _atr(self, tr: float) -> float:
        """Wilder RMA of true range (Pine ta.atr), SMA-seeded."""
        n = self.cfg.length
        if self._atr_rma_value is None:
            self._atr_seed.append(tr)
            if len(self._atr_seed) >= n:
                self._atr_rma_value = sum(self._atr_seed) / len(self._atr_seed)
                self._atr_seed = []
                return self._atr_rma_value
            return sum(self._atr_seed) / len(self._atr_seed)
        self._atr_rma_value += (tr - self._atr_rma_value) / n
        return self._atr_rma_value

    # -- the per-bar step (shared by warm + on_bar) ------------------------------ #

    def _step(self, bar: Bar) -> tuple[Zone | None, list[str]]:
        """Update every zone/FVG with one closed bar, then evaluate the spawn.
        Returns (zone spawned this bar | None, degraded list)."""
        c = self.cfg
        self._i += 1
        degraded = [
            "regime(VIX 252d rank — 0.5 neutral)",
            "mtf(60m baseline unwarmable intraday — 0.5 neutral)",
            "vrp(index−dailyRV — 0.5 neutral)",
        ]

        # Fast (1-bar) pivots — prior bar as the apex (evaluate before append).
        if len(self._bars) >= 2:
            h1, h2 = self._bars[-1].high, self._bars[-2].high
            l1, l2 = self._bars[-1].low, self._bars[-2].low
            if h1 > h2 and h1 > bar.high:
                self._last_ph = h1
            if l1 < l2 and l1 < bar.low:
                self._last_pl = l1

        # FVG spawn checks the bar two back — before append, that's _bars[-2].
        fvg_bull = len(self._bars) >= 2 and bar.low > self._bars[-2].high
        fvg_bear = len(self._bars) >= 2 and bar.high < self._bars[-2].low
        if fvg_bull or fvg_bear:
            if fvg_bull:
                f = Fvg(top=bar.low, bottom=self._bars[-2].high, is_bull=True,
                        start_idx=self._i)
            else:
                f = Fvg(top=self._bars[-2].low, bottom=bar.high, is_bull=False,
                        start_idx=self._i)
            while len(self.fvgs) >= c.max_fvg:
                self.fvgs.pop(0)
            self.fvgs.append(f)

        self._bars.append(bar)
        prev_close = self._bars[-2].close if len(self._bars) >= 2 else bar.close
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        atr = self._atr(tr)

        # ---- update the FVG lifecycle: OPEN → CE(50%) → FILLED (purge filled).
        still: list[Fvg] = []
        for f in self.fvgs:
            if f.state == FVG_FILLED:
                continue                      # purged the bar after it filled
            filled = bar.low <= f.bottom if f.is_bull else bar.high >= f.top
            ce_hit = bar.low <= f.mid if f.is_bull else bar.high >= f.mid
            if filled:
                f.state = FVG_FILLED          # reported once, dropped next bar
            elif ce_hit and f.state == FVG_OPEN:
                f.state = FVG_CE
            still.append(f)
        self.fvgs = still

        # ---- update the zone state machine (purge zones broken last bar).
        live: list[Zone] = []
        for z in self.zones:
            if z.state == BROKEN:
                continue                      # reported once, dropped next bar
            z.reacted_this_bar = False
            breached = bar.close < z.bottom if z.is_bull else bar.close > z.top
            in_now = bar.low <= z.top and bar.high >= z.bottom
            reacted = (
                (bar.low <= z.top and bar.close > z.top) if z.is_bull
                else (bar.high >= z.bottom and bar.close < z.bottom)
            )
            if breached:
                z.state = BROKEN
                z.inside = False
            elif in_now:
                # Distinct-entry touch counting (the INTENT of Pine's counter —
                # its literal code only counted the first entry from ACTIVE).
                if not z.inside:
                    z.touches += 1
                z.inside = True
                z.state = MITIGATED if reacted else TESTED
                z.reacted_this_bar = reacted
            else:
                z.inside = False
            live.append(z)
        self.zones = live

        # ---- spawn: raw gate + the 8-component score.
        body = abs(bar.close - bar.open)
        rng = bar.high - bar.low
        bodies = [abs(b.close - b.open) for b in self._bars]
        avg_body = sum(bodies) / len(bodies) if bodies else 0.0
        vols = [b.volume for b in self._bars]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        vol_warm = len(self._bars) >= c.length and avg_vol > 0
        if not vol_warm:
            degraded.append("volume_baseline(cold — cVol=0.5 neutral)")

        rel_body = body / avg_body if avg_body > 0 else 0.0
        rel_rng = rng / atr if atr > 0 else 0.0
        rel_vol = bar.volume / avg_vol if avg_vol > 0 else 0.0
        close_pos = (bar.close - bar.low) / rng if rng > 0 else 0.5

        is_bull = bar.close > bar.open
        is_bear = bar.close < bar.open
        body_ok = rel_body >= c.body_mult
        atr_ok = rel_rng >= c.atr_mult
        raw_bull = is_bull and body_ok and atr_ok and close_pos >= c.close_thresh
        raw_bear = is_bear and body_ok and atr_ok and (1.0 - close_pos) >= c.close_thresh

        spawned: Zone | None = None
        if raw_bull or raw_bear:
            bull = raw_bull
            brk = (
                (self._last_ph is not None and bar.close > self._last_ph) if bull
                else (self._last_pl is not None and bar.close < self._last_pl)
            )
            score = self._score(bull, rel_body, rel_rng, rel_vol, vol_warm, close_pos, brk)
            if score >= c.min_score:
                top = max(bar.open, bar.close)
                bottom = min(bar.open, bar.close)
                overlap = c.overlap_filter and any(
                    z.state != BROKEN and z.is_bull == bull
                    and top >= z.bottom and bottom <= z.top
                    for z in self.zones
                )
                if not overlap:
                    while len(self.zones) >= c.max_zones:
                        self.zones.pop(0)     # FIFO, oldest out (the Pine shift)
                    spawned = Zone(
                        top=top, bottom=bottom, is_bull=bull, score=score,
                        start_idx=self._i,
                    )
                    self.zones.append(spawned)
        return spawned, degraded

    def _score(
        self, bull: bool, rel_body: float, rel_rng: float, rel_vol: float,
        vol_warm: bool, close_pos: float, brk: bool,
    ) -> float:
        """The transparent 8-component spawn score (weights sum to 100).
        Unsourceable components pin to the 0.5 neutral midpoint (degraded)."""
        c = self.cfg
        c_body = _norm_ratio(rel_body / c.body_mult) * W_BODY
        c_atr = _norm_ratio(rel_rng / c.atr_mult) * W_ATR
        pos = close_pos if bull else 1.0 - close_pos
        c_close = _convict(pos, c.close_thresh) * W_CLOSE
        c_struct = (1.0 if brk else 0.0) * W_STRUCT
        c_vol = (_norm_ratio(rel_vol / c.vol_mult) if vol_warm else 0.5) * W_VOL
        c_regime = 0.5 * W_REGIME     # VIX 252d rank — unsourceable intraday
        c_mtf = 0.5 * W_MTF           # 60m displacement baseline — unwarmable
        c_vrp = 0.5 * W_VRP           # Pine's own not-applicable midpoint
        total = c_body + c_atr + c_close + c_struct + c_vol + c_regime + c_mtf + c_vrp
        return min(max(total, 0.0), 100.0)

    # -- per bar ------------------------------------------------------------------ #

    def on_bar(self, state: MarketState) -> ZoneReading | None:
        c = self.cfg
        if not c.enabled or not state.bars:
            return None
        cur = state.bars[-1]
        if self._last_bar_ts is not None and cur.ts == self._last_bar_ts:
            return None
        self._last_bar_ts = cur.ts

        spawned, degraded = self._step(cur)
        price = cur.close

        views: list[ZoneView] = []
        spawned_view: ZoneView | None = None
        relation, relation_zone = "CLEAR", None
        nearest_bull_below: ZoneView | None = None
        nearest_bear_above: ZoneView | None = None
        n_active = n_tested = 0
        for z in self.zones:
            v = ZoneView(
                top=z.top, bottom=z.bottom, is_bull=z.is_bull,
                score=round(z.score, 1), state=z.state, touches=z.touches,
                effective=round(z.effective(self._i, c.decay_bars), 1),
                age_bars=self._i - z.start_idx,
            )
            views.append(v)
            if z is spawned:
                spawned_view = v
            if z.state == BROKEN:
                continue
            if z.state == ACTIVE:
                n_active += 1
            else:
                n_tested += 1
            # Relation (priority: MITIGATING > TESTING > APPROACHING).
            if z.reacted_this_bar and relation != "MITIGATING":
                relation, relation_zone = "MITIGATING", v
            elif z.inside and relation not in ("MITIGATING", "TESTING"):
                relation, relation_zone = "TESTING", v
            elif relation == "CLEAR":
                dist = z.bottom - price if price < z.bottom else price - z.top
                if 0 < dist <= c.approach_pts:
                    relation, relation_zone = "APPROACHING", v
            # Nearest aligned zones for the arm legs.
            if z.is_bull and z.top <= price:
                if nearest_bull_below is None or z.top > nearest_bull_below.top:
                    nearest_bull_below = v
            if not z.is_bull and z.bottom >= price:
                if nearest_bear_above is None or z.bottom < nearest_bear_above.bottom:
                    nearest_bear_above = v

        note = (
            f"{n_active} active / {n_tested} tested zone(s), "
            f"{len(self.fvgs)} FVG(s) — {relation.lower()}"
        )
        return ZoneReading(
            zones=tuple(views),
            fvgs=tuple(
                Fvg(top=f.top, bottom=f.bottom, is_bull=f.is_bull,
                    state=f.state, start_idx=f.start_idx)
                for f in self.fvgs
            ),
            spawned=spawned_view,
            relation=relation,
            relation_zone=relation_zone,
            nearest_bull_below=nearest_bull_below,
            nearest_bear_above=nearest_bear_above,
            n_active=n_active,
            n_tested=n_tested,
            degraded=tuple(degraded),
            note=note,
        )
