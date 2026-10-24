---
name: helenus-signal-lifecycle
description: How a Helenus signal idea becomes a governed, graded, weighted (or retired) edge — idea capture, plan approval, flag-gated build with hand fixtures, replay, live grading, lessons, weight decision. Use when someone proposes a new signal/trigger/filter/confluence, asks "should we add X", or wants to promote/retire an existing one.
---

# Helenus signal lifecycle

## 1. Purpose
The promotion ladder from hunch to weighted confluence — Helenus's equivalent of a
hypothesis-to-candidate pipeline. Prevents "Claude invented a strategy" by making every
idea pass the same governed stages the confluence rework followed.

## 2. When to use
New signal/trigger/filter/modifier proposals; promotion requests ("make it a veto",
"raise its weight", "enable the flag"); retirement decisions.

## 3. When not to use
Evaluating evidence quality per se → `helenus-research-discipline`.
Executing the CURRENT validation work → `helenus-confluence-validation-campaign`.

## 4. Inputs required
The idea, its claimed mechanism (WHY it should lead or filter), and which lifecycle
stage it is at.

## 5. The ladder (each stage gates the next)

**Stage 0 — Idea capture.** Write the mechanism, not just the pattern: what dealer/flow
behavior makes it work, when it should fail, what it predicts BEFORE looking at data.
An idea without a falsifiable prediction stays a note.

**Stage 1 — Precedent check.** `helenus-failure-archaeology`: has grading already ruled?

**Stage 2 — Plan + owner approval.** One task, written plan, WAIT (change control).
The plan declares: config flag + default (usually off), where it sits in gate
precedence (or that it never initiates), what it may never do (veto? initiate?),
`degraded` behavior, and the validation metric.

**Stage 3 — Build, flag-gated + hand fixtures.** Pure engine, no I/O; standalone test
suite with hand-computable fixtures; legacy tests pinned untouched; boards (`!cmd`) and
journal fields added so validation is possible later. Pattern to copy: `engine/sq9.py`
(contract in docstring, bounded modifier, journal `sq9_*` back-tag fields).

**Stage 4 — Replay on real tape.** `scripts/replay_gate.py` (or a bespoke replay)
over `journal/tape_*.json`: firing frequency, noise, cooldown behavior. Record counts
in a durable note — e.g. the rework's Task 4 counts (6 LIQ_REJECT candidates filtered
from 30 raw rejection bars, 40 zone spawns) survive only in session memory, not in any
repo artifact: don't repeat that mistake.

**Stage 5 — Live, provisional.** Flag on only by owner decision; journal fields
accumulate; alerts tagged so the new axis is separable (`conv_signed`, `zone_relation`,
`sq9_*`, `pre_armed` are the existing patterns).

**Stage 6 — Back-tag + expectancy report.** Per-confluence expectancy conditioned
separately; veto test against graded winners; early-reversal companion metric; report
to owner. **Only the owner finalizes weights/defaults.**

**Stage 7 — Weighted / demoted / retired.** Outcomes are all legitimate: full
confluence weight; demotion to manual-read-only (the planned Sq9 fallback:
`modifier_enabled=False`); or retirement — documented in lessons.md with alert IDs,
flag left off, code retained until owner removes it.

## 6. Evidence required
Stage transitions carry artifacts: plan text (2), suite pass counts (3), replay counts
(4), journal field names (5), the report (6), owner's decision (7).

## 7. Output artifact
A lifecycle memo stating the idea's current stage, the artifact that put it there, and
the single next gate. Never skip-grade a stage.

## 8. Common traps
- **Bad behavior this prevents:** a session ports an indicator from a blog, wires it
  directly into `detect_candidate` ranked above displacement, and calls it an upgrade —
  skipping stages 0–2 and violating gate-precedence governance.
- Building the validation fields "later" — if stage 3 doesn't journal the axis, stage 6
  is impossible (the rework added `sq9_*` fields at build time deliberately).
- Treating stage 4 counts as performance (replay is unpriced, ungraded).
- Letting a provisional stage-5 default quietly become permanent without stage 6 —
  **this is the current live risk** (Sq9 weights, zone min_score=35).

## 9. Related skills
`helenus-research-discipline` (evidence tiers), `helenus-change-control` (stages 2–3),
`helenus-confluence-validation-campaign` (stages 6–7 for the rework),
`helenus-architecture-contract` (what a new engine may never do).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `grep -n "modifier_enabled\|min_score" helenus/config.py` (still provisional?)
- `git status --short` (rework still uncommitted?)

Last generated: 2026-07-05
Known uncertainty:
- Stage numbering is this skill's formalization of the owner's demonstrated workflow (confluence rework 2026-07-04/05); the owner has not ratified it as named stages.
