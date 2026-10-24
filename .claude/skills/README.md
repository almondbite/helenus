# Helenus skill library

Capability-transfer system distilled 2026-07-05 (Claude Fable 5 session, owner-guided).
Goal: let future weaker sessions (GPT 5.5 / Opus 4.8 / Sonnet-class) and junior/mid
engineers carry Helenus forward without re-deriving its architecture, discipline, or
scar tissue. Start here, then open the skill you need.

## First skills for a new session
1. `helenus-repo-orientation` — always, before trusting any doc.
2. `helenus-change-control` — before mutating anything.
3. The task-specific skill from the inventory below.
Currently mandated work: `helenus-confluence-validation-campaign`.

## Inventory

| Skill | One line | Invoke when |
|---|---|---|
| helenus-repo-orientation | Accurate map + which docs to trust (README is stale-risk) | session start; "where/what is X" |
| helenus-change-control | One task, plan+wait, flag-gated, fixtures; status-language gate | before any mutation; before saying done/validated |
| helenus-build-test-env | Global Py3.12, no pytest, PYTHONPATH standalone runners | running tests; setup; import errors |
| helenus-architecture-contract | The 11 invariants (purity, map-vs-trigger, Sq9/zones never initiate/veto…) | designing/reviewing engine changes |
| helenus-debugging-playbook | Symptom → discriminating check table for live/replay weirdness | "it's broken/quiet/wrong" |
| helenus-failure-archaeology | Settled battles + alert-ID evidence (ES veto, dead fields, absorption) | before re-proposing filters/vetoes; "was this tried?" |
| helenus-research-discipline | The 5-tier evidence ladder; small-n humility; the veto test | any "it works/validated" claim |
| helenus-signal-lifecycle | Idea → flag-gated build → replay → grade → owner report → weight | new signal proposals; promotions/retirements |
| helenus-confluence-validation-campaign | The hardest current problem, phased V0–V4 to an owner report | "continue/validate the rework"; new-knob questions |
| helenus-market-domain-reference | GEX/vanna/charm/CD/MOC mechanics + Schwab quirks, signs first | any market-mechanics reasoning (top weak-model risk) |
| helenus-diagnostics-tooling | replay/grade/ingest scripts, ! boards, journal queries | "measure it"; retrospectives; artifact locations |
| helenus-validation-and-qa | Test tiers, legacy pins, fixture rules, ceiling of green suites | writing tests; assertion failures; test-diff review |
| helenus-docs-and-writing | Append-only corrections, status vocabulary, report templates | editing README/lessons; writing reports |
| helenus-research-frontier | Real open problems + anti-goals, falsifiable success criteria | "what next"; ambitious pitches |
| helenus-analyst-prompting | _SEC_* assembly, cache discipline, Signal schema, cost contract | touching analyst.py/prompt/cost |

## Dependency map
- change-control gates everything that mutates; research-discipline gates everything
  that concludes.
- signal-lifecycle = change-control × research-discipline over time; the campaign skill
  is its instantiation for the 2026-07 rework.
- failure-archaeology feeds research-discipline (precedent) and debugging-playbook (scars).
- architecture-contract and market-domain-reference underpin all engine work
  (structure vs mechanics).
- build-test-env → validation-and-qa → research-discipline is the run→prove→conclude chain.
- analyst-prompting depends on architecture-contract invariants 2 and 9.

## Automatic vs manual invocation
Safe for automatic model triggering: all 15 (none mutates anything by itself; each
gates rather than acts). Highest-stakes, read carefully rather than skim:
change-control, research-discipline, confluence-validation-campaign — these three stop
the historically costly failure modes (scope creep, premature validation claims,
stale-doc trust). Manual-only artifacts: `_model_transfer_eval.md` (run deliberately),
`scripts/ingest_today.py` guidance in diagnostics-tooling (mutating, owner-gated).

## Single-home rule (anti-drift)
Some facts appear in several skills as one-line reminders; each has ONE authoritative
home where the detail lives — update the home, keep the echoes terse:
- Environment truth (global Python, no pytest, no venv, PYTHONPATH) → `helenus-build-test-env`.
- Flag defaults + provisional rework values (min_score=35, Sq9 weights, itm delta 0.65,
  fire_line 60) → `helenus/config.py` itself; skill-side detail in `helenus-change-control`'s table.
- Evidence-tier numbering → `helenus-research-discipline` (validation-and-qa lists test
  TYPES, deliberately unnumbered).
- Graded-alert evidence and alert IDs → `helenus-failure-archaeology/reference.md`.
- Gate precedence + invariants → `helenus-architecture-contract`.

## Meta files
`_distillation_plan.md` (Phase 0 contract) · `_repo_capability_map.md` (verified repo
survey) · `_expert_distillation_notes.md` (heuristics, red flags, decision trees,
lessons) · `_trigger_matrix.md` (routing rules) · `_model_transfer_eval.md` (12-task
benchmark) · `_review_factual.md` / `_review_doctrine.md` / `_review_usability.md` +
`_fixer_report.md` (Phase 6 QA) · `_uncertainty_register.md` (honest unknowns) ·
`_maintenance_plan.md` (how to keep all this true).

## Changelog
- 2026-07-05: initial library (15 skills), authored + tri-reviewed + fixed in one
  distillation session.
