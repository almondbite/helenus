# Failure archaeology — full stories

Source of record: `journal/lessons.md` (curated 2026-06-25 from 26 graded alerts,
34.6% accurate) + `journal/helenus.jsonl` + module docstrings. Alert IDs are queryable
in the jsonl.

## 1. The ES-imbalance saga (veto → demotion)
- **Symptom:** several graded losers showed a strong opposing /ES order-book imbalance
  (|imbalance| > ~0.4): 47374a6c (ES −0.556 vs bullish thrust, −31 MAE), 1a250eb4
  (ES +0.913 vs a short, −17 MAE), 14ac62a9 (ES +0.44 vs short), MIXED 14436eee (+0.5),
  06febcff (−0.5).
- **Wrong path:** promote opposing-ES to a hard veto (it was drafted as one).
- **Reconciliation (2026-06-25, hand-curated):** the two largest blowups had
  near-neutral ES (cc861b57 −33 MAE at ES +0.176; b0bd0bf8 −25 MAE), and fresh-low
  winner 78ab2967 (+21.8 MFE) fired AGAINST ES +0.63 and won.
- **Doctrine:** moderate priced penalty, overridable by the fresh-extreme with-trend
  profile; "cut it, don't veto on it."
- **Reusable rule:** before hardening any filter into a veto, run it against every
  graded WINNER and show it blocks none (or accept the cost explicitly).

## 2. The 7340–7350 absorption cluster (lagging triggers)
- **Symptom:** repeated afternoon shorts into an already-held session-low shelf; each
  break was quietly absorbed and graded INACCURATE.
- **Wrong path:** tighten reactive thresholds so shorts fire "later/safer" — still lagging.
- **Root cause:** confirmation triggers cannot see aggression being absorbed; only
  executed flow can.
- **Doctrine:** `engine/cumdelta.py` — CD divergence as (a) post-verdict veto/conviction
  cap on entries INTO absorption and (b) an early reversal ARM ranked above the lagging
  triggers. Stream-only by construction; flag default-off pending owner.

## 3. Confidence-band non-discrimination
- 53–66 confidence carried winners and losers alike across the graded set. The
  structural profile (fresh extreme, with-trend, negative net-GEX, room, breadth)
  discriminated instead. Doctrine in lessons.md Tuning notes: reserve >60 for the full
  aligned profile; push mid-range/stacked-wall setups below 50 or off.

## 4. Dead/weak fields discovered by grading
- `vanna_active`: False on 28/29 alerts (all but 06febcff), including every Vanna
  Momentum winner → no usable signal.
- `otm_call_put_ratio ≤ 1`: did NOT predict failure; winners 6752da44 (0.89),
  c19bdb9c (0.79), 69b53bf7 (0.84), 1f93e950 (0.95).
- Flow magnitude: loser 65744777 had the most extreme one-sided flow recorded
  (29,548 vs 0) — room + above-VWAP location is the discriminator, not magnitude.

## 5. The winner profile (what actually graded ACCURATE)
Fresh-extreme WITH-TREND breaks in negative net-GEX with open air ahead:
6752da44 (opening drive, +41 MFE), 624d3944 (7400 wall flip), 78ab2967 (fresh-low Put
Flow Pressure, +21.8), 155957a5 (fresh-low range expansion), c19bdb9c (ORB at fresh
high). Standalone Vanna Momentum with >30pt overhead room ABOVE VWAP: 95fafc39 (+29.8),
1f93e950 (+27.4). EMA Ignition at fresh high reclaiming a wall with ~20pt air: 69b53bf7 (+26).

## 6. The loser profile
Mid-range reclaims/thrusts, especially extended ~4–5× ATR or below VWAP (9d0d2470,
b0bd0bf8, 65744777); longs just under an overhead magnet with thin room (962aaef4 at
2.37pt, 9fb6cf50 at 7pt); shorts into stacked put-wall clusters (c0fd93fa, 8.6pt room,
MFE 0.3); displacement that lost its 50% midpoint with no sweep/MSS (4627a704).

## 7. Data-source scars
- **SPX volume:** raw feed produced a 10.98× SPCFD artifact → SPY cumulative-volume
  deltas at bar boundaries only (`bot._bar_volume`, `helenus/bot.py`; conviction.py
  "VOLUME REWIRING").
- **$SPXW as underlying:** chains endpoint 400s; query `$SPX` and filter roots
  client-side (`_filter_contract_root`) — also excludes the AM-settled monthly on
  shared-date 3rd Fridays.
- **VIX1D EOD ramp:** measurement window shrinks into the close → mechanical rise →
  vanna suppressed in the late window (`FlowConfig.vix1d_distort_*`).
- **Schwab streams:** no cash-index stream exists; $SPX tape is poll-folded (~15s) —
  the documented reason es_lead and the stream layer exist at all.

## 8. Governance scars
- README drift: the rework (2026-07-04/05) is uncommitted and the README still
  describes the legacy stack — sessions must check the tree first.
- REGIME_FLIP demoted to a muted unscored info embed (rework Task 1, owner decision).
- Zone `min_score=35` and the Sq9 modifier weights are provisional/uncalibrated —
  flagged for the validation phase, not settled facts.
