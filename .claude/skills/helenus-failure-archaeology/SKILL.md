---
name: helenus-failure-archaeology
description: Helenus's memory of past failures and settled battles — the ES-imbalance veto-vs-demotion saga, the 7340-7350 absorption cluster, dead journal fields (vanna_active), the confidence-band failure, the SPX volume artifact. Use before re-proposing a filter/veto/threshold idea, when a lessons.md claim needs its evidence, or when something feels like it must have been tried before.
---

# Helenus failure archaeology

## 1. Purpose
Prevent re-fighting settled battles. Git history is uninformative ("Add changes"); the
real record is `journal/lessons.md`, `journal/helenus.jsonl`, and module docstrings.
This skill indexes it.

## 2. When to use
- Proposing any new filter, veto, or threshold — check whether graded evidence already
  ruled on it.
- A lessons.md claim needs its underlying alert IDs.
- "Why is X built this weird way?" — often the scar tissue of a documented failure.

## 3. When not to use
Live triage → `helenus-debugging-playbook`. Fresh evidence evaluation →
`helenus-research-discipline`.

## 4. Inputs required
`journal/lessons.md` and read access to `journal/helenus.jsonl` (query by alert `id`).

## 5. The settled battles (do not re-fight without NEW graded evidence)

1. **ES order-book imbalance is a demotion, NOT a veto.** A hard veto would have
   blocked winner 78ab2967 (+21.8 MFE, fired against ES +0.63) while the two worst
   losers (cc861b57 −33 MAE, b0bd0bf8 −25 MAE) had near-neutral ES. Hand-reconciled
   2026-06-25 in lessons.md. Re-opening this requires new alert IDs that change the picture.
2. **The 7340–7350 absorption cluster** → confirmation triggers lag by construction;
   answer was cumulative-delta divergence (`engine/cumdelta.py`), a leading read ranked
   above the lagging triggers — not tighter reactive thresholds.
3. **Numeric confidence (53–66 band) did not discriminate** winners from losers.
   Structural filters did. Don't build logic on the confidence number without recalibration.
4. **`vanna_active` is an effectively dead journal field** — False on 28 of 29 alerts
   (all but 06febcff), including every Vanna Momentum winner. Carries no usable signal;
   don't condition on it. (lessons.md rounds this to "every alert".)
5. **Flow magnitude is not conviction.** The most one-sided flow on record (65744777,
   29,548 vs 0) lost mid-range/below-VWAP; room (>30pt) + above-VWAP carried the winners
   (95fafc39 +29.8, 1f93e950 +27.4).
6. **`otm_call_put_ratio ≤ 1` does not predict failure** — fresh-extreme longs won
   routinely with ocpr<1 (6752da44, c19bdb9c, 69b53bf7, 1f93e950). Don't resurrect it as a gate.
7. **Stacked same-side walls absorb beyond the letter of the room rule** — c0fd93fa met
   8pt at 8.6pt and failed into three stacked put walls.
8. **Raw SPX volume is unusable** (10.98× SPCFD artifact) — SPY proxy only, read at bar
   boundaries. Encoded in `conviction.py`'s volume rewiring and scan2's `_bar_volume`.
9. **VIX1D end-of-day ramp is mechanical**, not vol — the vanna trigger is suppressed
   late-day by design.
10. **REGIME_FLIP was demoted** (rework Task 1) from a scored trigger to a muted
    unscored info embed — re-promoting it is an owner decision, not a bug fix.

Full stories (symptom → wrong path → evidence → doctrine) in `reference.md`.

## 6. Procedure
1. Extract the proposal's mechanism ("veto shorts when X").
2. Grep lessons.md + this index for the mechanism and its variables.
3. If a settled battle covers it: cite the entry + alert IDs, and require NEW graded
   evidence to reopen (with the would-it-have-blocked-a-winner check).
4. If not covered: it's genuinely new → route to `helenus-signal-lifecycle`.
5. Query raw evidence: `python -c "import json;[print(json.dumps(r)) for r in map(json.loads,open('journal/helenus.jsonl',encoding='utf-8')) if r.get('id')=='<ID>']"`

## 7. Evidence required
Every archaeology citation carries its alert IDs or lessons.md line. "I recall" is not a citation.

## 8. Output artifact
A precedent note: proposal → matching settled battle(s) → verdict (blocked-by-precedent /
reopen-with-new-evidence / genuinely-new).

## 9. Common traps
- **Bad behavior this prevents:** a session sees opposing ES imbalance on three recent
  losers and re-implements the hard veto that graded evidence already rejected.
- Treating lessons.md as immutable truth — it's the CURRENT doctrine, curated
  2026-06-25 from 26 graded alerts (34.6% accurate baseline; the journal has since
  grown to 29 outcomes); new evidence updates it via dated appends.
- Reading significance into single-digit-n splits.

## 10. Related skills
`helenus-research-discipline`, `helenus-signal-lifecycle`, `helenus-docs-and-writing`
(how to append a correction).

## Provenance and maintenance

Re-verify volatile claims with:
- `head -3 journal/lessons.md` (still the 2026-06-25 curation? more alerts graded since?)
- `python -c "import json,collections;print(collections.Counter(json.loads(l)['type'] for l in open('journal/helenus.jsonl',encoding='utf-8')))"`

Last generated: 2026-07-05
Known uncertainty:
- Journal counts (29/29/6) will grow; per-battle doctrine may be superseded by later dated curation in lessons.md — the file header carries the date.
