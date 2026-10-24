---
name: helenus-docs-and-writing
description: Helenus documentation house style — append-only lessons.md corrections citing alert IDs, dated curation headers, status language (known/observed/candidate/unverified/provisional/stale-risk), README drift marking, journal record conventions. Use when writing or editing README, lessons.md, reviews, reports, or any status claim.
---

# Helenus docs and writing

## 1. Purpose
Keep the docs of record trustworthy: evidence-linked, append-only where graded history
is involved, and honest about status. Owner-ranked failure mode #2 is sessions trusting
stale docs — this skill is how docs stop going stale silently.

## 2. When to use
Editing `README.md` or `journal/lessons.md`; writing validation reports, review
records, or memos; any sentence containing done/fixed/verified/validated.

## 3. When not to use
Deciding what the evidence MEANS → `helenus-research-discipline`.

## 4. Inputs required
The doc, the evidence (alert IDs / commands / artifacts), and change-control approval
for docs of record.

## 5. House style

**Status vocabulary (mandatory):** known (verified this session, cite how) · observed
(read in repo, not executed) · candidate · provisional (owner-approved pending
validation — e.g. Sq9 weights) · unverified · stale-risk · owner-confirmation-needed.
Forbidden without an artifact: fixed, verified, validated, done, proven, confirmed.

**lessons.md conventions (mirror the existing file):**
- Header line carries the curation date + graded-alert count + baseline accuracy.
- Every behavioral claim cites alert IDs inline (`78ab2967 (+21.8)`), with MFE/MAE numbers.
- Corrections are APPENDED as dated hand-curation notes explaining what was
  reconciled and why (see the 2026-06-25 ES-imbalance note: "demotion, not veto") —
  never silently rewrite a prior lesson. Superseded guidance stays visible.
- Sections: What works / What misses / Tuning notes. Tuning notes state penalties vs
  vetoes explicitly.
- This file is LOADED INTO THE ANALYST PROMPT at runtime — a wrong lesson steers live
  judgment. Edits are engine-adjacent changes: plan + owner approval.

**journal/helenus.jsonl:** append-only, machine-written. Humans/models never edit
records; back-tag analysis goes in separate files.

**README:** narrative doc, currently stale-risk (pre-rework). When updating (owner-
approved): update the gate stack, scalp mode, commands list, and Setup to match
reality; where something is intentionally deferred, keep the "Known structural
placeholders" pattern. When you FIND drift but aren't approved to fix it, report it —
don't patch it as a side effect.

**Reports/memos (validation, reviews):** lead with the decision the reader must make;
every number carries n + tier + IDs; a "what this does NOT show" section is mandatory;
end with explicit uncertainties.

Templates: `templates/lessons_correction.md`, `templates/validation_report.md`.

## 6. Evidence required
A doc change asserting behavior needs the same evidence as the behavior claim itself.

## 7. Output artifact
The edited doc (approved) or a drift report (unapproved), plus the evidence links.

## 8. Common traps
- **Bad behavior this prevents:** a session "tidies" lessons.md, deleting the
  ES-imbalance reconciliation note as redundant — destroying the only record of WHY
  the veto was rejected, and changing what the live analyst reads.
- Editing the lessons block wording in a way that invalidates prompt-cache assumptions
  is fine (lessons load AFTER the cached block) — but moving lessons INTO the cached
  methodology block is not; see `helenus-analyst-prompting`.
- Marking something "verified" because a doc says verified (doc ≠ artifact).

## 9. Related skills
`helenus-change-control`, `helenus-research-discipline`, `helenus-failure-archaeology`.

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `head -3 journal/lessons.md` (current curation date/count)
- `git diff --stat README.md` (drift being fixed?)

Last generated: 2026-07-05
Known uncertainty:
- Whether the owner wants README updated now is owner-confirmation-needed (memory notes: "user hasn't asked").
