---
name: helenus-research-frontier
description: Helenus's open problems, extension paths, and anti-goals — auto-execution / auto-trading requests, "bolt on an ML model", and other ambitious pitches route here to hit the anti-goals list, alongside the real frontier (confidence calibration, small-n, cash-index tape gap, per-contract exit signals, restart persistence). Use when asked "what should we work on next", when someone proposes automating trade execution or training models on the journal, or when scoping ambitious ideas.
---

# Helenus research frontier

## 1. Purpose
Point ambition at the real open problems instead of re-solving settled ones, with
falsifiable success criteria for each.

## 2. When to use
"What's next / what would make this better / can we add ML / new ideas" conversations.

## 3. When not to use
The CURRENT mandated work is the validation campaign — that outranks everything here.
Anything here still enters through `helenus-signal-lifecycle` stage 0–2.

## 4. Inputs required
Awareness that all of these are OWNER-approval-gated ideas, not backlog items.

## 5. Open problems (ranked by asset-vs-gap)

1. **Confidence calibration.** The 53–66 band didn't discriminate (settled). Asset: a
   graded journal with structural features per alert. First steps: (a) define the
   structural profile score from lessons.md as a deterministic feature, (b) back-test it
   against graded outcomes as a *reported* companion to confidence, (c) propose a
   recalibration mapping for owner review. **Result when:** the new score separates
   ACCURATE from INACCURATE on forward alerts materially better than the confidence band
   (state n; no significance theater).
2. **Small-n forever.** ~dozens of Claude-judged alerts/session, 29 graded total —
   per-axis cells will stay thin. First steps: (a) make back-tagging repeatable over
   every stored tape, (b) accumulate tapes daily (`replay_today`), (c) grade
   gate-candidates counterfactually (what the gate WOULD have fired) to grow n without
   spending Claude calls. **Result when:** an expectancy table whose cells grow by
   construction each session. Caveat: counterfactual candidates lack verdicts — label
   them as gate-level, not signal-level, evidence.
3. **The cash-index tape gap.** $SPX is poll-folded (~15s); Schwab streams no cash
   index (settled constraint). Paths: SPY×ratio synthetic tape, or /ES-anchored bar
   assembly. Danger: silently changing every downstream ATR/extreme. **Result when:** a
   flag-gated alternate tape reproduces session H/L/ATR within stated tolerance on
   stored tapes, and at least one graded-miss cluster (late bars) demonstrably improves.
4. **Per-contract exit signals** (scalp Phase 2, documented deferral in README/scalp):
   take-profit spike, vega extraction, volume-absorption exits need streamed
   per-contract candles — the stream layer now exists; scalp premium is still
   poll-cadence (documented follow-up). **Result when:** exit signals computed on
   streamed candles for the selected contract, fixture-tested, and journaled alongside
   entries for grading.
5. **Restart persistence** (known placeholders): in-flight excursion tracking and
   `vix_history` are memory-only. Small, well-scoped, owner-flagged as "wire before
   going live". **Result when:** a restart mid-session loses no maturing grades on a
   deliberate test.
6. **Lessons distillation robustness.** lessons.md is hand-curated from Claude reviews;
   there is no check that a lesson still holds as n grows. **Result when:** each lesson
   carries a machine-checkable predicate re-evaluated as outcomes accumulate, flagging
   decayed lessons for re-curation.

### Standing anti-goals (don't propose)
Auto-execution; an "ML model that predicts direction" bolted on without the lifecycle;
replacing the deterministic grader with model judgment; new data vendors without owner
cost review.

## 6. Evidence required
Any frontier pitch: which problem, the asset it builds on, stage-0 falsifiable
prediction, and the failure condition that would kill it.

## 7. Output artifact
A stage-0/1 lifecycle memo per `helenus-signal-lifecycle` — never code first.

## 8. Common traps
- **Bad behavior this prevents:** "I added a logistic-regression confidence model
  trained on the 29 alerts and it hits 78% in-sample" — small-n overfit, skipped
  lifecycle, and a settled-battle reprise (confidence).
- Solving #3 by quietly repointing the tape (every engine's calibration shifts).

## 9. Related skills
`helenus-signal-lifecycle`, `helenus-research-discipline`,
`helenus-confluence-validation-campaign` (the actual current priority).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `grep -n "Known structural placeholders" -A 12 README.md`
- `head -3 journal/lessons.md` (current n)

Last generated: 2026-07-05
Known uncertainty:
- Rankings are this session's judgment; owner priorities may differ — confirm before campaigning.
