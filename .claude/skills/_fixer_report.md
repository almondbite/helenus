# Fixer report (Phase 6)

Date: 2026-07-05. Inputs: `_review_factual.md` (0 BLOCKING / 2 IMPORTANT / 9 MINOR),
`_review_doctrine.md` (1 BLOCKING / 5 IMPORTANT / 7 MINOR), `_review_usability.md`
(1 BLOCKING / 10 IMPORTANT / 5 MINOR). All BLOCKING and IMPORTANT findings applied;
MINOR findings applied opportunistically or explicitly deferred below.

## BLOCKING — fixed
- **Doctrine B1** (lessons.md gating contradiction): `helenus-change-control` table
  rewritten — docs of record (README, lessons.md) now require plan + owner approval
  with the live-prompt rationale stated; `journal/helenus.jsonl` got its own
  never-edit row; non-record docs split out.
- **Usability F-01** (frontier safety net can't trigger): `helenus-research-frontier`
  description now leads with auto-execution / auto-trading / "bolt on an ML model"
  vocabulary.

## IMPORTANT — fixed
- **Doctrine I1**: change-control plan-first rule now defines non-trivial ("any change
  under helenus/, tests/, scripts/, or a doc of record; if unsure, it isn't trivial").
- **Doctrine I2**: config-value row now states every `config.py` value is a shipped
  default → owner-gated; experiments use offline overrides.
- **Doctrine I3**: build-test-env setup annotates `python main.py` as live/owner-only.
- **Doctrine I4 + Usability F-11**: campaign Phase V1 rewritten — concrete back-tag
  recipe (tape → MarketState.push_bar → axis fields at alert ts), verified
  `replay_today.py` today-only constraint, past-date script extension routed through
  plan approval.
- **Doctrine I5**: scratch analysis rerouted to the session scratchpad in both the
  campaign and diagnostics-tooling; `.claude/skills/` explicitly named doctrine store,
  not workspace.
- **Factual IMP-1** (`legacy_gex()` misattribution): corrected in change-control,
  validation-and-qa, architecture-contract, and `_repo_capability_map.md`
  (`gex_mode()`/`gexmap_enabled()` live in test_gex_decouple.py; `legacy_gex()` in
  test_scan2/test_approach/test_es_lead).
- **Factual IMP-2** (`scan2._bar_volume`): corrected to `bot._bar_volume`
  (`helenus/bot.py`) in failure-archaeology/reference.md and expert notes.
- **Usability F-02**: campaign description narrowed to evaluation/calibration with
  explicit sibling handoffs (math → market-domain-reference; new signals → lifecycle).
- **Usability F-03 / Doctrine M1**: malformed change-control eval row flipped to
  `should_trigger:false` (routes to docs-and-writing), matching the matrix semantics.
  All 15 JSONs re-validated (21 entries each, parse clean).
- **Usability F-05**: matrix rule 7 added (startup auth → build-test-env; mid-session
  staleness → debugging-playbook).
- **Usability F-06**: campaign Phase V4 now cites
  `helenus-docs-and-writing/templates/validation_report.md`; matrix rule 8 added.
- **Usability F-07**: validation-and-qa "tiers" renamed to "test types" with an
  explicit pointer that tier numbering lives only in research-discipline.
- **Usability F-08/F-09**: "Single-home rule" section added to README.md naming the
  authoritative home for environment facts, flag defaults, tier numbering, alert-ID
  evidence, and precedence.

## MINOR — fixed opportunistically
- vanna_active precision (28/29, all but 06febcff) in archaeology SKILL.md +
  reference.md + expert notes red-flag table.
- VIX1D distortion window corrected ~15:30 → 15:45 (market-domain, debugging-playbook).
- 22 → 21 config classes (capability map).
- `ESLeadConfig.enabled=False` added to the default-off flag lists (change-control,
  expert notes, distillation plan).
- Gate precedence, GEX-formula, `alert_id` linkage, and `dispx_enabled` uncertainties
  RESOLVED in their skills' Known-uncertainty blocks (verified by factual review).
- `replay_gate.py` hardcoded-tape note and `replay_today.py` today-only note in
  diagnostics-tooling.
- `journal/` gitignored / machine-only warning added to repo-orientation and
  diagnostics-tooling.
- Signal vs `_SIGNAL_SCHEMA` distinction in analyst-prompting.
- Task-4 replay counts marked memory-sourced (signal-lifecycle, campaign) with the
  "30 raw rejection bars" phrasing corrected.
- 26-vs-29 graded-alert relationship spelled out in failure-archaeology.

## MINOR — deferred (documented, deliberate)
- Provenance-section heading numbering inconsistency ("## 10." vs "##") across skills —
  cosmetic; the block is always last and always titled "Provenance and maintenance".
- Slightly-low negative ratios (28.6%) in 4 eval files — above the ≥1/3 target in 11 of
  15; acceptable per usability's own threshold note; add real-world misses over time
  per `_maintenance_plan.md`.
- Deliberate repetitions flagged as such by usability review (safety-critical one-liners
  like "never tune in production") — left in place by design; homes declared in README.
- `claude-api` as an expected_skill (external to this library) — intentional: it exists
  in the harness skill list.

## Verification after fixes
- `python -c "import json,glob; ..."` — all 15 trigger_eval.json parse, 21 entries each.
- Spot re-read of every edited SKILL.md section.
