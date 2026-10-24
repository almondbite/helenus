# Skill-library maintenance plan (Phase 7)

## Re-verification schedule
- **Every session that mutates the repo:** if your change touches a fact a skill states
  (flag default, precedence, command, path), update that skill in the SAME session and
  bump its `Last generated` date.
- **After each lessons.md re-curation:** refresh `helenus-failure-archaeology`
  (SKILL.md index + reference.md) and the journal counts quoted anywhere
  (grep: `grep -rn "29 " .claude/skills --include=SKILL.md`).
- **After the validation campaign completes:** rewrite
  `helenus-confluence-validation-campaign` (it becomes archaeology: what was decided,
  with the report as evidence), update provisional-weight language in change-control,
  signal-lifecycle, research-discipline, and the README index.
- **Monthly (or on onboarding a new model tier):** run every skill's own
  "Provenance and maintenance" commands; they are one-liners by design.

## Drift-check commands
```bash
git status --short                      # is the rework committed yet? (many skills key on this)
python --version; python -m pytest --version   # environment contract still global/no-pytest?
PYTHONPATH=. python tests/test_gex.py   # canonical suite still green/runnable
grep -n "enabled: bool" helenus/config.py      # flag defaults vs what skills claim
head -3 journal/lessons.md              # curation date + graded-alert count
ls tests/ | wc -l                       # suite count (skills say 22 as of 2026-07-05)
```

## Adding a new skill
1. Folder `.claude/skills/<name>/SKILL.md`, frontmatter `name` + trigger-rich
   `description` (most important trigger terms first).
2. Sections 1–10 as in the existing skills, including when-NOT-to-use, a
   bad-behavior-prevented example, and the Provenance block with re-verify one-liners.
3. `evals/trigger_eval.json` — ≥20 realistic queries, ≥1/3 negatives with the correct
   sibling in `expected_skill`.
4. Add a row to `_trigger_matrix.md` (check sibling collisions) and a line to
   `README.md` (inventory + dependency map).
5. Verify every command/path in the skill against the repo before writing it, or label
   it `unverified`.

## Retiring a stale skill
Do not delete silently: move the folder's SKILL.md content essence into
`helenus-failure-archaeology/reference.md` if it holds lessons, delete the folder, and
note the retirement (what/why/date) in `README.md`'s changelog section and in
`_uncertainty_register.md` if claims remain unverified.

## Updating trigger evals
When a skill misfires in real use (triggered wrongly, or failed to trigger), add that
exact real query to its `trigger_eval.json` with the correct routing — the eval files
are meant to accumulate real-world misses, not stay pristine.

## Re-running model-transfer evals
`_model_transfer_eval.md` — run all 12 tasks in a fresh session of the target model
(with and without skill access), score per the rubric, and record results as a dated
section appended to that file. Re-run after: a new model tier arrives, a major library
revision, or the validation campaign changes the doctrine.

## Ownership
The skill library is repo-adjacent documentation: changes to it are docs-class under
`helenus-change-control` (no owner approval needed to keep facts current; owner
approval needed to change encoded DOCTRINE — evidence bar, discipline rules,
anti-goals).
