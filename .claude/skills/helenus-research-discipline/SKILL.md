---
name: helenus-research-discipline
description: The Helenus evidence bar for signal research — what counts as validated, small-n humility, fixture-green vs replay vs graded-journal tiers, no live tuning, veto-vs-demotion testing. Use when evaluating whether a signal/filter/threshold "works", when someone says validated/proven/high win rate, or before changing any weight or threshold based on results.
---

# Helenus research discipline

## 1. Purpose
Preserve the project's evidence bar: hypothesis ≠ result, fixture-green ≠ live truth,
raw MFE ≠ tradeable edge, and 29 graded alerts ≠ statistics. Owner-confirmed bar for
the current rework: **back-tag + per-confluence expectancy report reviewed by the owner
BEFORE any weight finalizes.**

## 2. When to use
Evaluating any performance/efficacy claim; designing an experiment; being asked to
tune a threshold, finalize a weight, or promote a filter to a veto.

## 3. When not to use
Pure code correctness → `helenus-validation-and-qa`. Historical precedent lookup →
`helenus-failure-archaeology`.

## 4. Inputs required
The claim, its evidence tier (below), and the relevant journal/replay artifacts.

## 5. The evidence ladder (each tier proves ONLY its own row)

| Tier | Artifact | Proves | Does NOT prove |
|---|---|---|---|
| 1. Hand-fixture suite green | `PYTHONPATH=. python tests/test_x.py` → N passed | the math matches the spec | the signal has edge |
| 2. Replay on real tape | `scripts/replay_gate.py` over `journal/tape_*.json` | firing frequency/noise on one real session | profitability (replay isn't graded; has no volume/flow/GEX unless supplied) |
| 3. Back-tag vs graded journal | alert-ID-cited analysis of `helenus.jsonl` outcomes | directional association on the graded set | significance (n≈29 total; per-bucket single digits) |
| 4. Owner-reviewed expectancy report | per-confluence expectancy, conditioned separately | a governed weight decision may be made | that the edge persists live |
| 5. Forward live grading | new journal outcomes after the change | the decision's actual performance | — |

Rules:
- **State the tier with every claim.** "Works" without a tier is a red flag.
- **Small-n humility:** at n≈29 (per-trigger n in single digits), report directions and
  ranges, never point-estimate accuracy as settled ("62% win rate" from n=5 is noise).
- **Gross ≠ net analog:** raw MFE ignores early reversal, spread, and premium bleed —
  pair MFE with the early-reversal metric (`FeedbackConfig.early_window_bars`/`early_reversal_pts`).
  Earliness features are judged on MFE-up AND early-reversal-down, not raw hit rate.
- **Never tune in production** (README rule): threshold changes are derived offline
  against replay/journal, then go through change control.
- **The veto test:** any proposed hard gate must be run against every graded WINNER; if
  it blocks one, it ships as a priced demotion or not at all (the ES-imbalance doctrine).
- **Condition per confluence separately** — pooled expectancy hides which axis pays.
- **No hidden trials:** if you tried 6 thresholds and report the best, say 6. Threshold
  shopping against 29 alerts manufactures edge from noise.
- **External sources generate hypotheses only.** A paper/tweet/Pine script justifies
  building a flag-gated experiment — never a weight or a default.

## 6. Procedure for evaluating "X works"
1. Identify tier. 2. Check archaeology for a settled ruling. 3. Check n per bucket.
4. Run the veto test if it gates. 5. Check the early-reversal companion metric.
6. Write the finding with tier + IDs + n; route weight decisions to the owner report.

## 7. Evidence required
Alert IDs (tier 3+), commands + outputs (tiers 1–2), explicit n, explicit trial count.

## 8. Output artifact
An evidence memo: claim → tier → artifacts/IDs → n → what may and may not be concluded
→ recommended next tier.

## 9. Common traps
- **Bad behavior this prevents:** a session replays a new trigger on `tape_2026-06-22`,
  sees 6 clean-looking candidates, and reports the trigger "validated, recommend
  enabling" — tier 2 evidence sold as tier 4.
- Averaging MFE across triggers with different target scales.
- Quietly re-running with adjusted thresholds until the back-tag "confirms".

## 10. Related skills
`helenus-signal-lifecycle` (the ladder as a process), `helenus-failure-archaeology`,
`helenus-confluence-validation-campaign` (the live application), `helenus-change-control`.

## Provenance and maintenance

Re-verify volatile claims with:
- `python -c "import json,collections;print(collections.Counter(json.loads(l)['type'] for l in open('journal/helenus.jsonl',encoding='utf-8')))"` (current n)
- `grep -n "early_window_bars\|early_reversal_pts" helenus/config.py`

Last generated: 2026-07-05
Known uncertainty:
- The owner's evidence bar was confirmed 2026-07-05 for the confluence rework; whether the same bar applies to future experiments is owner-confirmation-needed (assume yes).
