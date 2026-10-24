---
name: helenus-change-control
description: Helenus change governance — one task at a time, plan first and wait for approval, additive + flag-gated, offline hand-fixture tests. Use BEFORE making any code, config-default, test, docs, or journal change; when tempted to bundle changes, enable a flag, "improve while here", or declare something done/fixed/validated.
---

# Helenus change control

## 1. Purpose
Encode the owner's mandated workflow so no session ships ungoverned change. Owner
confirmed (2026-07-05) these four rules are the core; scope creep past them is a
top-three historical failure mode.

## 2. When to use
Before ANY mutation: code, config defaults, tests, docs, lessons.md, or enabling flags.
Also when writing status words: done / fixed / verified / validated.

## 3. When not to use
Pure read/analysis tasks (but its status-language rules still apply to what you report).

## 4. Inputs required
The requested change, current `git status`, and which config flag (if any) governs the area.

## 5. Procedure — the four rules
1. **One task at a time.** Do the asked task. Adjacent problems are REPORTED, not fixed.
   New ideas become notes for the owner, not commits.
2. **Plan first, WAIT for approval.** For any non-trivial change, present a plan and
   stop. Do not begin building on "probably fine". (This is the owner's explicit
   workflow from the confluence rework: ONE task, plan, wait.) Any change under
   `helenus/`, `tests/`, `scripts/`, or a doc of record is non-trivial by definition;
   if you're unsure whether something is trivial, it isn't.
3. **Additive + flag-gated.** New behavior ships behind a config flag with the legacy
   path exactly preserved when the flag is off. Default state of the flag is an OWNER
   decision. Legacy tests get pinned (see `gex_mode()`/`gexmap_enabled()` in
   `tests/test_gex_decouple.py`; `legacy_gex()` in `tests/test_scan2.py`,
   `test_approach.py`, `test_es_lead.py`; `legacy_scalp()` in `tests/test_scan2.py`) —
   if a legacy test must change to pass, the change is not additive: stop and re-plan.
4. **Offline hand-fixture tests.** Every engine change carries standalone tests with
   hand-computable fixtures (no network, no Schwab key), runnable via
   `PYTHONPATH=. python tests/test_<x>.py`.

### Change classification
| Class | Gate |
|---|---|
| Docs-only, NON-record (code comments, scratch analysis, this skill library) | May proceed; never let docs promote a claim's status (see `helenus-docs-and-writing`) |
| Docs of record (README, `journal/lessons.md`) | **Plan + owner approval.** lessons.md is loaded into the live analyst prompt — editing it changes live judgment. Corrections are append-only, dated, alert-ID-cited |
| `journal/helenus.jsonl` | Machine-written, append-only; humans/models never edit records |
| Test-only (new fixtures) | May proceed if legacy tests untouched |
| Engine code | Plan → approval → flag-gated → fixtures green |
| Config **value** (ANY `config.py` edit) | Every `config.py` value is a shipped default → owner decision. Experiments pass overrides in offline analysis scripts instead; proposals derive from replay/journal evidence, never live tuning |
| Config **default / flag flip** | Owner decision, always. Includes `GexMapConfig.enabled`, `CumDeltaConfig.enabled`, `ApproachConfig.enabled`, `ESLeadConfig.enabled`, `FlowConfig.inflection_enabled` (all default off) and `Sq9Config` weights (provisional) |
| Weight finalization (rework) | Blocked until back-tag + per-confluence expectancy report reviewed by owner |

### Status language gate
Say **done/fixed** only with the command output or artifact path in hand. Say
**validated** only when the owner-reviewed evidence bar is met. Otherwise use:
candidate / observed / unverified / provisional.

## 6. Evidence required
Before "done": the test command + its pass count; for behavior claims, the replay or
journal artifact. Before "no regression": legacy-pinned suites green, flags off.

## 7. Output artifact
A plan message (pre-approval) or a change summary listing: task, files touched, flag
guarding it, tests run with counts, and explicitly what was NOT done.

## 8. Common traps
- **Bad behavior this prevents:** while adding a zone field, a session notices the
  README is stale and rewrites it, flips `min_score` to a "better" value, and reports
  everything "validated" — three governance violations in one diff.
- Editing a legacy test so the new behavior passes (breaks the additive contract).
- Treating an uncommitted working tree as license to refactor — the rework diff is the
  owner's work in progress; don't "clean it up".
- Committing/pushing without being asked (git history here is owner-managed).

## 9. Related skills
`helenus-validation-and-qa` (what tests prove), `helenus-signal-lifecycle` (promotion
ladder), `helenus-docs-and-writing` (status language), `helenus-confluence-validation-campaign`
(the currently gated decision).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `git status --short` (is the rework still uncommitted?)
- `grep -n "enabled: bool = False" helenus/config.py` (which flags are still default-off)

Last generated: 2026-07-05
Known uncertainty:
- Owner may flip flag defaults after validation; re-read `helenus/config.py` rather than trusting the table above.
