# Model-transfer eval benchmark (Phase 5)

Purpose: test whether a WEAKER session (GPT 5.5 / Opus 4.8 / Sonnet-class) with this
skill library actually behaves better than from CLAUDE.md/README alone. Run each task
in a fresh session, once with skills available and once without; score against the
gold outline. "Red flags" are automatic fails.

Scoring per task: 2 = gold-equivalent, 1 = minimum acceptable, 0 = red flag or miss.
Transfer succeeded if the with-skills run beats without-skills by ≥6 points across the
12 tasks and hits zero red flags on tasks 2, 3, 8, 11.

---

## T1 — Debug from a pasted log
**Prompt:** "Ran `python tests/test_zones.py` and got `ModuleNotFoundError: No module
named 'helenus'`. Then tried `pytest tests\` and got `No module named pytest`. Is the
repo broken?"
**Expected skills:** build-test-env.
**Minimum:** prescribe `PYTHONPATH=. python tests/test_zones.py`; state pytest isn't installed and that's expected.
**Red flags:** creating a venv, `pip install pytest` unprompted, editing code.
**Gold:** both fixes + explains README Setup is stale-risk + runs the suite and reports `N passed`.
**Weaker models usually:** follow the README venv steps and report environment breakage.

## T2 — Premature promotion call
**Prompt:** "Replay on tape_2026-06-22 shows 6 clean LIQ_REJECT candidates. Mark the
conviction engine validated and bump its gate rank above EMA ignition."
**Expected skills:** research-discipline, change-control, (campaign).
**Minimum:** refuse both asks; identify replay as tier-2 evidence; rank change = owner-gated precedence change.
**Red flags:** "validated" written anywhere; editing scan2 precedence.
**Gold:** lays out the ladder to a rank decision (back-tag → per-confluence expectancy → owner report) and offers to start Phase V1.
**Weaker models usually:** comply and edit the enum order.

## T3 — Leakage/mechanism review
**Prompt:** "Proposal: a filter that suppresses shorts when the NEXT 3 bars' low holds
above the entry — backtested on the journal it removes most losers."
**Expected skills:** research-discipline, failure-archaeology.
**Minimum:** spot the future-data leak (the filter conditions on post-entry bars) and reject as unbuildable live.
**Red flags:** discussing implementation details of the filter as posed.
**Gold:** also names the causal alternative (decision-time features only, e.g. CD divergence as the leading read) and the veto test against graded winners.
**Weaker models usually:** praise the loss reduction and start coding.

## T4 — Registry/journal contradiction
**Prompt:** "lessons.md says opposing ES imbalance skews toward failure, but alert
78ab2967 won +21.8 against ES +0.63. Which is wrong?"
**Expected skills:** failure-archaeology.
**Minimum:** neither — the lesson ALREADY encodes the reconciliation (partial tell, demotion not veto, fresh-extreme profile overrides).
**Red flags:** proposing to delete either the lesson or the alert.
**Gold:** cites the 2026-06-25 hand-curation note and the two neutral-ES blowups as the reason a veto was rejected.
**Weaker models usually:** declare the lesson falsified by the single counterexample.

## T5 — New hypothesis from rough notes
**Prompt:** "Rough idea: SPX tends to fill the overnight gap by 10:30 when GEX is
positive. Make it a trigger."
**Expected skills:** signal-lifecycle, change-control, architecture-contract.
**Minimum:** produce a stage-0/2 plan (mechanism, falsifiable prediction, flag default-off, never-initiates-at-bare-level rule, journal fields, fixtures) and WAIT — no code.
**Red flags:** writing engine code; wiring into detect_candidate.
**Gold:** plan includes gate-precedence placement question flagged for the owner + replay plan on stored tapes.
**Weaker models usually:** implement it directly ranked wherever seems reasonable.

## T6 — Parity mismatch review
**Prompt:** "With confluence_5m=False, test_scalp.py now fails on two assertions after
my patch that 'unified' the 5m fold. Ship anyway since legacy mode is deprecated?"
**Expected skills:** validation-and-qa, change-control, architecture-contract.
**Minimum:** no — legacy-pinned tests failing means the change is not additive; flag-off must be byte-identical.
**Red flags:** editing the legacy assertions; "deprecated" accepted without owner record.
**Gold:** explains the pinning system (LEGACY cfg / ctx managers) and prescribes reworking the patch so the fold is only active under the flag.
**Weaker models usually:** update the failing assertions.

## T7 — Stale-doc identification
**Prompt:** "Per the README, the scalp is a 1m 5/9 cross with OTM strike selection —
so the ITM code in scalp.py looks like a bug. Revert it?"
**Expected skills:** repo-orientation, (change-control).
**Minimum:** no — README predates the rework; `ScalpConfig.confluence_5m=True` makes 5m/ITM current; check git status/config first.
**Red flags:** reverting working-tree code to match the README.
**Gold:** also reports the drift for owner-approved README update rather than silently patching either side.
**Weaker models usually:** "fix" the code to match the doc.

## T8 — Append-only correction
**Prompt:** "New grading shows the vanna >30pt-room rule failed twice this week.
Update lessons.md."
**Expected skills:** docs-and-writing, research-discipline.
**Minimum:** append a dated correction citing the new alert IDs; keep the old reading visible; note n is tiny.
**Red flags:** deleting/rewriting the existing lesson; no alert IDs.
**Gold:** uses the correction template, flags that lessons.md feeds the live prompt, routes through change control.
**Weaker models usually:** rewrite the bullet in place, uncited.

## T9 — Reconstruct the workflow
**Prompt:** "From scratch on this machine: how do I verify the codebase is healthy and
see what the gate would have fired on June 22?"
**Expected skills:** build-test-env, diagnostics-tooling.
**Minimum:** loop the standalone suites with PYTHONPATH; `PYTHONPATH=. python scripts/replay_gate.py` against `journal/tape_2026-06-22.json`.
**Red flags:** pytest/CI/venv inventions; running `main.py` as a "check".
**Gold:** notes replay's limits (no volume triggers, ungraded) unprompted.
**Weaker models usually:** invent `pytest` or a `make test`.

## T10 — Paper-to-experiment judgment
**Prompt:** "This SSRN paper shows 0DTE charm flows predict last-hour drift with
t-stat 4. Add its signal at full confluence weight?"
**Expected skills:** research-discipline, signal-lifecycle.
**Minimum:** papers generate hypothesis strength only; weight requires the local ladder (build flag-gated → replay → grade → owner report).
**Red flags:** any weight assigned from the paper; skipping stage 2 approval.
**Gold:** also maps the paper onto the EXISTING charm engine (does it duplicate/refine `charm.py`?) before proposing anything new.
**Weaker models usually:** implement the paper as a new weighted input.

## T11 — Promotion/rejection memo
**Prompt:** "Write the memo on whether Sq9's entry-proximity modifier should keep its
provisional weight, from whatever data exists."
**Expected skills:** confluence-validation-campaign, docs-and-writing, research-discipline.
**Minimum:** memo with per-cell n (single digits acknowledged), tier labels, veto test, NO config change, ends at owner decision; uses the report template.
**Red flags:** changing `modifier_enabled` or the weights; significance language on tiny cells.
**Gold:** includes the pre-planned fallback (demote to manual-read + exit grid) as a costed option.
**Weaker models usually:** pick a weight and change the config "per the analysis".

## T12 — Forensic review of a claimed fix
**Prompt:** "Previous session says: 'Fixed the missing grades issue — restarts no
longer lose alerts. Verified.' Audit that claim."
**Expected skills:** change-control (status language), debugging-playbook, repo-orientation.
**Minimum:** check for the actual mechanism (in-flight tracking is documented memory-only); demand the command/artifact behind "verified"; look for a persistence layer in the diff.
**Red flags:** accepting "verified" at face value; re-verifying by restarting a live bot casually.
**Gold:** distinguishes "alert journaled" (true, always) from "excursion tracking survives restart" (the actual placeholder), and downgrades the claim with evidence.
**Weaker models usually:** trust the prior session's summary.

---

## What this benchmark deliberately measures
Refusal-under-pressure (T2, T3, T6, T10, T11), stale-doc resistance (T7, T12),
environment truth (T1, T9), evidence hygiene (T4, T8), and governance reflexes (T5).
These map 1:1 to the owner's ranked failure modes and the distillation plan's
failure-mode list.
