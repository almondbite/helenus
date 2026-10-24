"""
Displacement conviction — the MATH of the Pine "Displacement Oscillator v6",
ported as pure Python. Formulas only; none of the Pine runtime (no ta.*,
request.security, barstate, drawing, timeframe auto-calibration) survives here.

What this is inside Helenus: an INPUT to the structural scoring engine, not a
standalone brain. The signed −100..+100 conviction score ENRICHES candidates
(the displacement notes, Claude's snapshot, the journal); only the liqRej
sweep-reject reversal — price sweeps the prior N-bar extreme intra-bar and
closes back inside, the documented personal-edge pattern — initiates its own
gate candidate (scan2 TriggerType.LIQ_REJECT), and Claude still judges it.

The score (weights are the Pine constants; the fire line/thresholds are the
Pine auto-cal "scalp" values, matching the 1m tape):

  BASE  = body·18·hvAdj + ATR-expansion·13 + max(EMA-trend·12 − no-break·3, 0)
        + RSI-band·9 + volume·8 + HV-vs-avg·8 + close-position·7 + VWAP-side·6
        + ATR-regime·5 + liquidity·8 + delta-flow·6
  BOOST = clamp(efficiency + FVG×ATR·8 + streak(peak at 2, penalty ≥4)
        + structure-break·10 + squeeze(+9 fired / −3 on), −7, +32)
  score = clamp(BASE + BOOST, 0, 100);  convSigned = ±score (winning side)

VOLUME REWIRING (raw SPX volume is garbage — the observed 10.98× SPCFD
artifact): bar volume is the SPY proxy already carried on MarketState bars,
with Pine's own useVol=false fallback (0.6× weight) while the baseline is
cold; the order-flow delta components run on ES CUMULATIVE-DELTA interval
diffs (engine/cumdelta via the /ES stream), scoring 0 + a `degraded` entry
when the stream is off. Other explicit degrades (never faked, always listed in
`ConvictionReading.degraded`): hvBodyAdj = 1.0 (needs a 252-day HV rank),
structure break = intraday fast-pivot only (no prior-day H/L history), and the
MTF ladder = 1m native + 5m/15m folds only (60m/D unsourceable intraday; a
folded bias reads 0 until its slow EMA warms).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from dataclasses import dataclass, field

from helenus.config import CONFIG, ConvictionConfig
from helenus.engine.scan2 import Bar, Direction, MarketState

# --------------------------------------------------------------------------- #
# The Pine score constants (the formula, not tuning — deliberately not config)
# --------------------------------------------------------------------------- #

W_BODY = 18.0
W_ATR = 13.0
W_TREND = 12.0
W_RSI = 9.0
W_VOL = 8.0
W_HV = 8.0
W_CLOSE = 7.0
W_VWAP = 6.0
W_REGIME = 5.0
W_LIQ = 8.0
W_DELTA = 6.0

Q_EFF = 8.0
Q_FVG = 8.0
Q_STREAK = 12.0
Q_STRUCT = 10.0
Q_CHOP = 7.0
Q_SQZ = 9.0
BOOST_CAP = 32.0


def _norm(x: float, r: float) -> float:
    """Pine `norm(x, r)`: 0..1 saturation at the threshold ratio."""
    if r <= 0:
        return 0.0
    return min(x / r, 1.0)


def _streak_pts(n: int) -> float:
    """Streak scoring: peak at 2 consecutive displacement candles (fresh
    momentum), decay after — 4+ suggests an extended move prone to reversion."""
    if n == 1:
        return Q_STREAK * 0.5
    if n == 2:
        return Q_STREAK
    if n == 3:
        return Q_STREAK * 0.35
    if n >= 4:
        return -Q_CHOP * 0.5
    return 0.0


def _eff_pts(eff: float) -> float:
    if eff >= 0.7:
        return Q_EFF
    if eff >= 0.5:
        return Q_EFF * 0.6
    if eff >= 0.35:
        return Q_EFF * 0.3
    if eff < 0.22:
        return -Q_CHOP
    return 0.0


def _stdev(xs: list[float]) -> float:
    """Population stdev (Pine ta.stdev's default)."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / n)


# --------------------------------------------------------------------------- #
# Small recursive indicators
# --------------------------------------------------------------------------- #

class _Ema:
    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.value: float | None = None
        self.n = 0

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.value + self.alpha * (x - self.value)
        self.n += 1
        return self.value

    @property
    def mature(self) -> bool:
        return self.n >= self.period


class _Rma:
    """Wilder smoothing (Pine ta.rma / the ATR kernel): SMA seed over the first
    `period` samples, then y += (x − y) / period."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.value: float | None = None
        self._seed: list[float] = []

    def update(self, x: float) -> float:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) >= self.period:
                self.value = sum(self._seed) / len(self._seed)
                self._seed = []
            return sum(self._seed) / len(self._seed) if self._seed else self.value
        self.value += (x - self.value) / self.period
        return self.value


class _Rsi:
    """Wilder RSI: RMA of gains / losses."""

    def __init__(self, period: int) -> None:
        self._up = _Rma(period)
        self._dn = _Rma(period)
        self._prev: float | None = None
        self.value: float = 50.0

    def update(self, close: float) -> float:
        if self._prev is not None:
            change = close - self._prev
            up = self._up.update(max(change, 0.0))
            dn = self._dn.update(max(-change, 0.0))
            if up is not None and dn is not None:
                self.value = 100.0 if dn == 0 else 100.0 - 100.0 / (1.0 + up / dn)
        self._prev = close
        return self.value


class _TfBias:
    """One rung of the MTF ladder: folds the 1m tape into `minutes` buckets
    (close-on-stamp, the scalp.py idiom) and reads the Pine f_bias — EMA 8>21>50
    with close above the slow EMA → +1, mirror → −1, else 0. Reads 0 until the
    slow EMA has warmed on that timeframe (no synthetic history — the honest
    degrade for intraday folds)."""

    def __init__(self, minutes: int, fast: int, mid: int, slow: int) -> None:
        self.minutes = minutes
        self.e_fast = _Ema(fast)
        self.e_mid = _Ema(mid)
        self.e_slow = _Ema(slow)
        self._key: dt.datetime | None = None
        self._close: float | None = None
        self.last_close: float | None = None

    def push(self, bar: Bar) -> None:
        shifted = bar.ts - dt.timedelta(minutes=1)
        floor_min = (shifted.minute // self.minutes) * self.minutes
        key = shifted.replace(minute=floor_min, second=0, microsecond=0)
        if self._key is not None and key != self._key and self._close is not None:
            self._fold(self._close)          # gap fallback: flush the stale bucket
        self._key = key
        self._close = bar.close
        if bar.ts.minute % self.minutes == 0:
            self._fold(bar.close)
            self._key = None
            self._close = None

    def _fold(self, close: float) -> None:
        self.e_fast.update(close)
        self.e_mid.update(close)
        self.e_slow.update(close)
        self.last_close = close

    @property
    def bias(self) -> int:
        if not self.e_slow.mature or self.last_close is None:
            return 0
        f, m, s = self.e_fast.value, self.e_mid.value, self.e_slow.value
        if f > m > s and self.last_close > s:
            return 1
        if f < m < s and self.last_close < s:
            return -1
        return 0


# --------------------------------------------------------------------------- #
# The per-bar reading
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ConvictionReading:
    score_bull: float
    score_bear: float
    conv_signed: float                       # −100..+100, the hero number
    tier: str                                # A+ / A / B / C on |conv|
    fire_bull: bool
    fire_bear: bool
    # The sweep-reject reversal (the personal-edge piece) — separately exposed.
    liq_rej_bull: bool
    liq_rej_bear: bool
    swept_low: float | None                  # the prior extreme a sweep ran
    swept_high: float | None
    mtf_sum: int
    net_flow: float | None                   # ES-delta sum over flow_len (contracts)
    components: dict = field(default_factory=dict)   # every s*/q* term, per side
    degraded: tuple[str, ...] = ()
    note: str = ""


def _tier(score: float) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    return "C"


# --------------------------------------------------------------------------- #
# The stateful engine (held by the bot)
# --------------------------------------------------------------------------- #

class ConvictionEngine:
    """Owns the indicator state; `on_bar(state, cum_delta)` once per closed tape
    bar → ConvictionReading (None when disabled). `warm(bars)` seeds every
    indicator from the backfill without producing fires."""

    def __init__(self, cfg: ConvictionConfig | None = None) -> None:
        self.cfg = cfg or CONFIG.conviction
        c = self.cfg
        n_keep = max(c.atr_regime_len, c.squeeze_len, c.hv_len, c.length + 1) + 10
        self._bars: deque[Bar] = deque(maxlen=n_keep)
        self._atr = _Rma(c.length)
        self._atr_hist: deque[float] = deque(maxlen=c.atr_regime_len)
        self._trs: deque[float] = deque(maxlen=c.squeeze_len)
        self._logrets: deque[float] = deque(maxlen=c.hv_len)
        self._hvs: deque[float] = deque(maxlen=c.hv_len)
        self.e_fast = _Ema(c.ema_fast)
        self.e_mid = _Ema(c.ema_mid)
        self.e_slow = _Ema(c.ema_slow)
        self._rsi = _Rsi(c.rsi_len)
        # 1m bars per RTH year — the Pine barsPerYear() for the annualized HV.
        bar_secs = CONFIG.scan.bar_seconds
        self._bpy = max(252.0 * 23400.0 / max(float(bar_secs), 1.0), 252.0)
        # Fast (1-bar) pivots — the only structure source (prior-day H/L degraded).
        self._last_ph: float | None = None
        self._last_pl: float | None = None
        # Displacement-candle streaks.
        self._streak_bull = 0
        self._streak_bear = 0
        # Squeeze edge detector.
        self._sqz_prev = False
        # ES cumulative-delta rewiring.
        self._prev_cd: float | None = None
        self._deltas: deque[float] = deque(maxlen=max(c.flow_len, c.length))
        # MTF ladder: 1m native + 5m/15m folds (60m/D omitted — degraded).
        self._mtf5 = _TfBias(5, c.ema_fast, c.ema_mid, c.ema_slow)
        self._mtf15 = _TfBias(15, c.ema_fast, c.ema_mid, c.ema_slow)
        # Fire cooldown, per direction, in bar index.
        self._i = -1
        self._last_bull_fire: int | None = None
        self._last_bear_fire: int | None = None
        self._last_bar_ts: dt.datetime | None = None

    # -- warmup -------------------------------------------------------------- #

    def warm(self, bars: list[Bar]) -> None:
        """Seed every indicator from the backfilled tape (no fires emitted)."""
        for b in bars:
            self._ingest(b, cum_delta=None)
        if bars:
            self._last_bar_ts = bars[-1].ts

    # -- per-bar ingestion (shared by warm + on_bar) -------------------------- #

    def _ingest(self, bar: Bar, cum_delta: float | None) -> dict:
        """Update all indicator state with one closed bar; returns the raw
        per-bar metrics the scorer consumes."""
        c = self.cfg
        self._i += 1
        prev_close = self._bars[-1].close if self._bars else bar.close

        # Fast pivots read the PRIOR bar as the apex, so evaluate before append.
        if len(self._bars) >= 2:
            h1, h2 = self._bars[-1].high, self._bars[-2].high
            l1, l2 = self._bars[-1].low, self._bars[-2].low
            if h1 > h2 and h1 > bar.high:
                self._last_ph = h1
            if l1 < l2 and l1 < bar.low:
                self._last_pl = l1

        # Liquidity sweep of the prior N-bar extreme (needs the full window so a
        # cold tape can't call every tick a "sweep").
        window = list(self._bars)[-c.length:]
        if len(window) >= c.length:
            prior_low = min(b.low for b in window)
            prior_high = max(b.high for b in window)
            sweep_l = bar.low < prior_low
            sweep_h = bar.high > prior_high
        else:
            prior_low = prior_high = None
            sweep_l = sweep_h = False

        fvg_up = len(self._bars) >= 2 and bar.low > self._bars[-2].high
        fvg_dn = len(self._bars) >= 2 and bar.high < self._bars[-2].low
        fvg_up_size = (bar.low - self._bars[-2].high) if fvg_up else 0.0
        fvg_dn_size = (self._bars[-2].low - bar.high) if fvg_dn else 0.0

        self._bars.append(bar)

        body = abs(bar.close - bar.open)
        rng = bar.high - bar.low
        tr = max(rng, abs(bar.high - prev_close), abs(bar.low - prev_close))
        atr = self._atr.update(tr) or 0.0
        self._atr_hist.append(atr)
        self._trs.append(tr)

        bodies = [abs(b.close - b.open) for b in list(self._bars)[-c.length:]]
        avg_body = sum(bodies) / len(bodies) if bodies else 0.0
        vols = [b.volume for b in list(self._bars)[-c.length:]]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        vol_warm = len(self._bars) >= c.length and avg_vol > 0

        rel_body = body / avg_body if avg_body > 0 else 0.0
        rel_rng = rng / atr if atr > 0 else 0.0
        rel_vol = bar.volume / avg_vol if avg_vol > 0 else 0.0
        close_pos = (bar.close - bar.low) / rng if rng > 0 else 0.5

        if prev_close > 0 and bar.close > 0:
            self._logrets.append(math.log(bar.close / prev_close))
        hv = _stdev(list(self._logrets)) * math.sqrt(self._bpy) * 100.0
        hv_avg = sum(self._hvs) / len(self._hvs) if self._hvs else hv
        self._hvs.append(hv)

        self.e_fast.update(bar.close)
        self.e_mid.update(bar.close)
        self.e_slow.update(bar.close)
        rsi = self._rsi.update(bar.close)
        self._mtf5.push(bar)
        self._mtf15.push(bar)

        # ATR-expansion regime (atr vs its 50-bar SMA).
        atr_sma = sum(self._atr_hist) / len(self._atr_hist) if self._atr_hist else 0.0
        atr_expand = atr_sma > 0 and atr / atr_sma >= c.atr_regime_mult

        # TTM-style squeeze: Bollinger 2σ compressed inside 1.5× Keltner.
        closes = [b.close for b in self._bars]
        if len(closes) >= c.squeeze_len and len(self._trs) >= c.squeeze_len:
            dev = c.squeeze_bb_mult * _stdev(closes[-c.squeeze_len:])
            kcr = sum(list(self._trs)[-c.squeeze_len:]) / c.squeeze_len
            sqz_on = dev < c.squeeze_kc_mult * kcr
        else:
            sqz_on = False
        sqz_fired = (not sqz_on) and self._sqz_prev
        self._sqz_prev = sqz_on

        # Efficiency ratio: net move / path length over the lookback.
        if len(closes) >= c.length + 1:
            net = abs(closes[-1] - closes[-(c.length + 1)])
            path = sum(
                abs(closes[i] - closes[i - 1]) for i in range(-c.length, 0)
            )
            eff = net / path if path > 0 else 0.0
        else:
            eff = None

        # Displacement-candle streaks (the oscillator's own candle definition).
        disp_bull = (
            bar.close > bar.open and rel_body >= c.body_mult
            and rel_rng >= c.atr_mult and close_pos >= c.close_thresh
        )
        disp_bear = (
            bar.close < bar.open and rel_body >= c.body_mult
            and rel_rng >= c.atr_mult and (1.0 - close_pos) >= c.close_thresh
        )
        if disp_bull:
            self._streak_bull += 1
            self._streak_bear = 0
        elif disp_bear:
            self._streak_bear += 1
            self._streak_bull = 0
        else:
            self._streak_bull = 0
            self._streak_bear = 0

        # ES cumulative-delta interval diff (the delta/netFlow rewiring).
        if cum_delta is not None:
            if self._prev_cd is not None:
                self._deltas.append(cum_delta - self._prev_cd)
            self._prev_cd = cum_delta
            delta = self._deltas[-1] if self._deltas else None
        else:
            self._prev_cd = None
            delta = None

        return {
            "bar": bar,
            "rel_body": rel_body,
            "rel_rng": rel_rng,
            "rel_vol": rel_vol,
            "vol_warm": vol_warm,
            "close_pos": close_pos,
            "rsi": rsi,
            "hv": hv,
            "hv_avg": hv_avg,
            "atr": atr,
            "atr_expand": atr_expand,
            "sqz_on": sqz_on,
            "sqz_fired": sqz_fired,
            "eff": eff,
            "fvg_up_q": (fvg_up_size / atr) if atr > 0 else 0.0,
            "fvg_dn_q": (fvg_dn_size / atr) if atr > 0 else 0.0,
            "sweep_l": sweep_l,
            "sweep_h": sweep_h,
            "swept_low": prior_low if sweep_l else None,
            "swept_high": prior_high if sweep_h else None,
            "brk_bull": self._last_ph is not None and bar.close > self._last_ph,
            "brk_bear": self._last_pl is not None and bar.close < self._last_pl,
            "delta": delta,
        }

    # -- the score ------------------------------------------------------------ #

    def _side_score(self, bull: bool, m: dict, vwap: float, delta_ma: float | None) -> tuple[float, dict]:
        c = self.cfg
        bar: Bar = m["bar"]
        is_dir = bar.close > bar.open if bull else bar.close < bar.open

        # hvBodyAdj needs the 252-day HV rank — unsourceable intraday: 1.0 (degraded).
        s_body = _norm(m["rel_body"], c.body_mult) * W_BODY * 1.0
        s_atr = _norm(m["rel_rng"], c.atr_mult) * W_ATR

        f, mid, s = self.e_fast.value, self.e_mid.value, self.e_slow.value
        aligned = (f > mid > s) if bull else (f < mid < s)
        half = (f > mid) if bull else (f < mid)
        s_tb = W_TREND if aligned else (W_TREND * 0.5 if half else 0.0)
        brk = m["brk_bull"] if bull else m["brk_bear"]
        s_tp = 0.0 if brk else -(W_TREND * 0.25)

        rsi = m["rsi"]
        if bull:
            s_rsi = (
                W_RSI if 50 <= rsi <= 72
                else W_RSI * 0.4 if rsi > 72
                else W_RSI * 0.6 if rsi >= 45
                else 0.0
            )
        else:
            s_rsi = (
                W_RSI if 28 <= rsi <= 50
                else W_RSI * 0.4 if rsi < 28
                else W_RSI * 0.6 if rsi <= 55
                else 0.0
            )

        # Volume: SPY proxy; Pine's own useVol=false fallback while cold.
        s_vol = _norm(m["rel_vol"], c.vol_mult) * W_VOL if m["vol_warm"] else W_VOL * 0.6
        s_hv = W_HV if m["hv"] > m["hv_avg"] else W_HV * 0.4
        pos = m["close_pos"] if bull else 1.0 - m["close_pos"]
        s_close = W_CLOSE if pos >= c.close_thresh else 0.0
        above_vwap = vwap == vwap and bar.close > vwap
        s_vwap = W_VWAP if (above_vwap if bull else not above_vwap) else 0.0
        s_regime = W_REGIME if m["atr_expand"] else W_REGIME * 0.3

        liq_rej = (m["sweep_l"] and bar.close > bar.open) if bull else (
            m["sweep_h"] and bar.close < bar.open
        )
        s_liq = W_LIQ if liq_rej else (
            W_LIQ * 0.4 if (m["sweep_l"] or m["sweep_h"]) else 0.0
        )

        # Delta: ES cum-delta interval flow; 0 when the stream is off (degraded).
        d = m["delta"]
        if d is None or delta_ma is None:
            s_delta = 0.0
        else:
            flow = (d > 0 and d > delta_ma) if bull else (d < 0 and d < delta_ma)
            s_delta = W_DELTA if flow else 0.0

        base = (
            s_body + s_atr + max(s_tb + s_tp, 0.0) + s_rsi + s_vol
            + s_hv + s_close + s_vwap + s_regime + s_liq + s_delta
        )

        s_eff = _eff_pts(m["eff"]) if m["eff"] is not None else 0.0
        s_fvg = min(m["fvg_up_q"] if bull else m["fvg_dn_q"], 1.0) * Q_FVG
        s_streak = _streak_pts(self._streak_bull if bull else self._streak_bear)
        s_struct = Q_STRUCT if brk else 0.0
        s_sqz = Q_SQZ if m["sqz_fired"] else (-3.0 if m["sqz_on"] else 0.0)
        boost = max(min(s_eff + s_fvg + s_streak + s_struct + s_sqz, BOOST_CAP), -Q_CHOP)

        score = min(max(base + boost, 0.0), 100.0)
        comps = {
            "body": round(s_body, 2), "atr": round(s_atr, 2),
            "trend": round(max(s_tb + s_tp, 0.0), 2), "rsi": round(s_rsi, 2),
            "vol": round(s_vol, 2), "hv": round(s_hv, 2),
            "close": round(s_close, 2), "vwap": round(s_vwap, 2),
            "regime": round(s_regime, 2), "liq": round(s_liq, 2),
            "delta": round(s_delta, 2), "eff": round(s_eff, 2),
            "fvg": round(s_fvg, 2), "streak": round(s_streak, 2),
            "struct": round(s_struct, 2), "squeeze": round(s_sqz, 2),
            "boost": round(boost, 2), "raw_dir": is_dir,
        }
        return score, comps

    # -- per bar --------------------------------------------------------------- #

    def on_bar(self, state: MarketState, cum_delta: float | None = None) -> ConvictionReading | None:
        c = self.cfg
        if not c.enabled or not state.bars:
            return None
        cur = state.bars[-1]
        if self._last_bar_ts is not None and cur.ts == self._last_bar_ts:
            return None
        self._last_bar_ts = cur.ts

        m = self._ingest(cur, cum_delta)
        vwap = state.vwap()
        delta_ma = (
            sum(self._deltas) / len(self._deltas) if self._deltas else None
        )

        score_bull, comps_bull = self._side_score(True, m, vwap, delta_ma)
        score_bear, comps_bear = self._side_score(False, m, vwap, delta_ma)
        conv = score_bull if score_bull >= score_bear else -score_bear

        degraded = ["hv_rank(252d — hvBodyAdj=1.0)", "prior_day_levels(struct=pivot-only)",
                    "mtf(60m/D omitted)"]
        if m["delta"] is None:
            degraded.append("es_delta(stream off — sDelta=0)")
        if not m["vol_warm"]:
            degraded.append("volume_baseline(cold — Pine useVol=false fallback)")

        # MTF ladder: 1m native + the 5m/15m folds.
        f, mid, s = self.e_fast.value, self.e_mid.value, self.e_slow.value
        bias_1m = 0
        if self.e_slow.mature:
            if f > mid > s and cur.close > s:
                bias_1m = 1
            elif f < mid < s and cur.close < s:
                bias_1m = -1
        mtf_sum = bias_1m + self._mtf5.bias + self._mtf15.bias

        liq_rej_bull = m["sweep_l"] and cur.close > cur.open
        liq_rej_bear = m["sweep_h"] and cur.close < cur.open

        raw_bull = (
            cur.close > cur.open and m["rel_body"] >= c.body_mult
            and m["rel_rng"] >= c.atr_mult and m["close_pos"] >= c.close_thresh
            and m["brk_bull"]
        )
        raw_bear = (
            cur.close < cur.open and m["rel_body"] >= c.body_mult
            and m["rel_rng"] >= c.atr_mult and (1.0 - m["close_pos"]) >= c.close_thresh
            and m["brk_bear"]
        )
        block_bull = c.mtf_filter and mtf_sum <= -c.mtf_block_sum
        block_bear = c.mtf_filter and mtf_sum >= c.mtf_block_sum
        cd_bull = self._last_bull_fire is None or (self._i - self._last_bull_fire) >= c.cooldown_bars
        cd_bear = self._last_bear_fire is None or (self._i - self._last_bear_fire) >= c.cooldown_bars
        fire_bull = raw_bull and score_bull >= c.fire_line and not block_bull and cd_bull
        fire_bear = raw_bear and score_bear >= c.fire_line and not block_bear and cd_bear
        if fire_bull:
            self._last_bull_fire = self._i
        if fire_bear:
            self._last_bear_fire = self._i

        net_flow = (
            sum(list(self._deltas)[-c.flow_len:]) if self._deltas else None
        )

        note = (
            f"conv {conv:+.0f} ({_tier(abs(conv))})"
            + (" FIRE ▲" if fire_bull else " FIRE ▼" if fire_bear else "")
            + (" liqRej▲" if liq_rej_bull else " liqRej▼" if liq_rej_bear else "")
        )
        return ConvictionReading(
            score_bull=round(score_bull, 2),
            score_bear=round(score_bear, 2),
            conv_signed=round(conv, 2),
            tier=_tier(abs(conv)),
            fire_bull=fire_bull,
            fire_bear=fire_bear,
            liq_rej_bull=liq_rej_bull,
            liq_rej_bear=liq_rej_bear,
            swept_low=m["swept_low"],
            swept_high=m["swept_high"],
            mtf_sum=mtf_sum,
            net_flow=net_flow,
            components={"bull": comps_bull, "bear": comps_bear},
            degraded=tuple(degraded),
            note=note,
        )
