# Usability review (Reviewer 3)

Reviewed 2026-07-05 from the perspective of a zero-context Sonnet-class session /
junior engineer. Scope: 15 skill folders + evals + meta files. Method: read all 15
SKILL.md files, both reference.md files, both templates, all 5 meta files; parsed all
15 trigger_eval.json files with Python; cross-checked eval routings against
`_trigger_matrix.md` and coverage against `_repo_capability_map.md`.

---

## 1. JSON validation results (explicit)

All 15 `evals/trigger_eval.json` files parsed successfully with `json.load`
(UTF-8). Uniform schema: list of `{query, should_trigger, expected_skill}`.

| Skill | Entries | Positive | Negative | Neg % | Bad skill refs |
|---|---|---|---|---|---|
| analyst-prompting | 21 | 14 | 7 | 33.3% | `claude-api` ×2 (see F-04) |
| architecture-contract | 21 | 13 | 8 | 38.1% | none |
| build-test-env | 21 | 14 | 7 | 33.3% | none |
| change-control | 21 | 14 | 7 | 33.3% | none (but see F-03) |
| confluence-validation-campaign | 21 | 13 | 8 | 38.1% | none |
| debugging-playbook | 21 | 14 | 7 | 33.3% | none |
| diagnostics-tooling | 21 | 15 | 6 | 28.6% | none |
| docs-and-writing | 21 | 15 | 6 | 28.6% | none |
| failure-archaeology | 21 | 14 | 7 | 33.3% | none |
| market-domain-reference | 21 | 15 | 6 | 28.6% | none |
| repo-orientation | 21 | 14 | 7 | 33.3% | none |
| research-discipline | 21 | 14 | 7 | 33.3% | none |
| research-frontier | 21 | 14 | 7 | 33.3% | none |
| signal-lifecycle | 21 | 13 | 8 | 38.1% | none |
| validation-and-qa | 21 | 15 | 6 | 28.6% | none |

All files meet the ≥20-entry bar. Phrasing is realistically messy (typos: "wont",
"idk", "??", lowercase, vague symptom reports) — genuinely good eval hygiene. All
`expected_skill` values except `claude-api` name real folders in the library.

---

## 2. Findings

### BLOCKING

**F-01 — research-frontier: the designated "dangerous pitch catcher" will not fire on
its own bait query.**
- Files: `helenus-research-frontier/SKILL.md` (frontmatter),
  `helenus-research-frontier/evals/trigger_eval.json`, `_trigger_matrix.md` (Notes).
- Issue: the matrix states frontier "deliberately CATCHES dangerous ambitious pitches
  (auto-execution, ML-on-29-alerts) so its anti-goals section fires before code gets
  written", and the eval contains `[POS] auto-execute the trades through schwab`. But
  the frontmatter description contains no auto-execution / trade-execution / "make it
  trade" vocabulary at all — it only offers "open problems", "what should we work on
  next", "ambitious ideas". A zero-context router sees "auto-execute the trades" as a
  build request, not a research-direction question, and will not load this skill. No
  sibling catches it either (architecture-contract's description doesn't mention
  execution; the capability map lists auto-execution only under "premature skills").
  The one safety mechanism the library explicitly designed for its worst-case query
  fails at the trigger layer, and the eval case will fail with it.
- Fix: add explicit terms to the frontier description, e.g. "…scoping ambitious ideas,
  or ANY proposal to auto-execute trades, place orders, or bolt on an ML direction
  predictor (these hit the standing anti-goals list)." Optionally add a matching
  anti-goal trigger note to architecture-contract as a second net.

### IMPORTANT

**F-02 — Campaign description over-claims all Sq9/zones/conviction traffic and will
collide with signal-lifecycle and market-domain-reference.**
- Files: `helenus-confluence-validation-campaign/SKILL.md` (frontmatter) vs
  `helenus-signal-lifecycle/evals/trigger_eval.json` ("what stage is the sq9 modifier
  at right now" → lifecycle POS) and
  `helenus-market-domain-reference/evals/trigger_eval.json` ("how far apart are sq9
  rings at spx 7400" → market-domain POS).
- Issue: campaign's description ends "…or anything touching Sq9/zones/conviction/5m-scalp
  evaluation". The single word "evaluation" is carrying all the disambiguation, and a
  Sonnet-class router will read "anything touching Sq9/zones/conviction" and fire
  campaign on Sq9 math questions, Sq9 stage questions, and zone-engine test failures —
  exactly the queries the sibling evals claim. The matrix (rule 1) only resolves
  campaign vs discipline/lifecycle for *doing the validation*, not for Sq9-mechanics
  or stage queries.
- Fix: rewrite the tail as "…or any request to tune/weight/judge the new
  Sq9/zones/conviction/5m-scalp knobs. NOT for Sq9/zone math (market-domain-reference)
  or new-signal proposals (signal-lifecycle)."

**F-03 — change-control eval breaks the library's own expected_skill semantics.**
- File: `helenus-change-control/evals/trigger_eval.json`, row
  `{"query": "add a dated correction to lessons.md for the vanna claim",
  "should_trigger": true, "expected_skill": "helenus-docs-and-writing"}`.
- Issue: `_trigger_matrix.md` documents that `expected_skill` names a sibling only on
  `should_trigger:false` rows. This is the only row in all 315 cases where a
  `should_trigger:true` row names a *different* skill. Presumably it encodes matrix
  rule 4 (change-control co-triggers on mutations), but the JSON schema has no
  co-trigger concept, so any eval harness either fails this row forever (if it asserts
  expected == self on positives) or silently accepts contradictory data. Either way
  the regression suite is unsound at this row.
- Fix: pick one semantic and enforce it — either set `expected_skill:
  "helenus-change-control"` (it should co-fire), or make it a negative routed to
  docs-and-writing, or extend the schema with `also_triggers: [...]` and document it
  in the matrix.

**F-04 — analyst-prompting eval references `claude-api`, which is not in this
library, and one negative contradicts the skill's own description.**
- File: `helenus-analyst-prompting/evals/trigger_eval.json` (2 rows), frontmatter.
- Issue (a): `expected_skill: "claude-api"` names a skill that exists only as a
  global harness skill, not under `.claude/skills/`. Deliberate per matrix rule 6,
  but any eval runner that validates expected_skill against the library folders will
  flag/crash on these rows, and the library ships no note telling it not to.
  Issue (b): the negative "what model id should we use for the analyst" is
  repo-specific and cost-affecting; the description says "Use when changing … anything
  affecting Claude-call cost". By the description this SHOULD fire (then consult
  claude-api for the model table). The eval and the description disagree, so one of
  them fails.
- Fix: (a) add a one-line note in `_trigger_matrix.md` that `claude-api` is an
  external/global skill and eval runners must whitelist it. (b) Flip that row to
  positive (analyst-prompting, deferring model-id facts to claude-api), or narrow the
  description's cost clause.

**F-05 — Schwab 401-at-startup is claimed by two skills with no matrix rule.**
- Files: `helenus-build-test-env/SKILL.md` + eval ("getting 401s from schwab at
  startup, token thing?" → build-test-env POS) vs `helenus-debugging-playbook/SKILL.md`
  frontmatter ("Schwab 400/401 errors") + symptom table row "401 / auth loop at
  startup".
- Issue: both descriptions explicitly claim 401/OAuth failures; the matrix's rule 3
  (won't-run vs assertion vs live) doesn't cover credentials. A router will
  legitimately pick either, so the build-test-env eval row is a coin flip. Mitigating:
  both skills prescribe the identical remedy (re-run `scripts/authorize.py`), so the
  user outcome is fine — this is eval/routing noise plus drift-prone duplication, not
  task failure.
- Fix: add a matrix rule ("credential/token errors → build-test-env; debugging-playbook
  row defers to it"), and make one of the two rows a pointer instead of a duplicate
  remedy.

**F-06 — "Write the validation report" is served ambiguously by two skills, and the
campaign never mentions the template built for it.**
- Files: `helenus-confluence-validation-campaign/SKILL.md` (Phase V4, Output artifact)
  vs `helenus-docs-and-writing/SKILL.md` + `templates/validation_report.md`.
- Issue: campaign eval claims "write the validation report for the owner" (POS);
  docs-and-writing eval claims "write the validation report - what format" (POS). The
  content-vs-format distinction is too subtle for a router — these will collide. Worse:
  `templates/validation_report.md` is literally titled "Phase V4 deliverable", yet the
  campaign SKILL.md (sections V4 and 7) never references it. A zero-context session
  executing the campaign — the library's flagship procedure — will invent a report
  format while a purpose-built template sits unreferenced in a sibling folder.
- Fix: campaign V4 should say "use
  `helenus-docs-and-writing/templates/validation_report.md`"; docs-and-writing keeps
  the template as its home. Adjust one eval row so only campaign claims the
  write-the-report intent.

**F-07 — Two different five-item "tier" tables will get conflated.**
- Files: `helenus-research-discipline/SKILL.md` §5 (evidence ladder: fixture → replay
  → back-tag → owner report → forward grading) vs `helenus-validation-and-qa/SKILL.md`
  §5 ("The tiers": fixtures, legacy pins, replay, journal, manual smoke).
- Issue: both are 5-row tables named "tiers"; validation-and-qa even says "a green
  suite is tier 1 of 5" (pointing at the discipline ladder) directly above its OWN
  differently-composed 5-row table. A zero-context model asked "what evidence tier is
  a replay run" can land on either table and answer "tier 2" vs "tier 3-of-a-different
  -taxonomy". The evidence ladder is the single most load-bearing concept in the
  library; it must have exactly one numbered form.
- Fix: research-discipline owns the numbered ladder. Rename validation-and-qa's table
  to "Test types" (unnumbered) and add "for the canonical evidence ladder see
  helenus-research-discipline §5".

**F-08 — The no-pytest/no-venv/PYTHONPATH environment facts live in ~7 files.**
- Files: `helenus-build-test-env` (home), `helenus-repo-orientation` (§5.3, §8),
  `helenus-validation-and-qa` (§5 "No pytest, no CI"), `helenus-change-control`
  (rule 4), `helenus-research-discipline` (ladder tier 1), `helenus-debugging-playbook`
  (§6), `helenus-diagnostics-tooling` (§4), plus `_repo_capability_map.md`.
- Issue: build-test-env's own Known-uncertainty section anticipates the owner
  installing pytest or creating a venv — at which point one file gets updated and six
  stale copies keep asserting "NO pytest". This is the exact silent-staleness failure
  the library warns about, self-inflicted. The bare run command
  (`PYTHONPATH=. python tests/test_x.py`) repeating is fine; the categorical
  environment claims ("no pytest exists", "no venv") are the drift risk.
- Fix: keep categorical environment claims only in build-test-env; siblings phrase as
  "run per helenus-build-test-env: `PYTHONPATH=. python tests/test_<x>.py`".

**F-09 — Provisional rework config values are restated in 4 skills.**
- Files: `helenus-confluence-validation-campaign` (V0: min_score=35,
  itm_target_delta=0.65, fire_line=60, Sq9 weights provisional),
  `helenus-change-control` (flag table), `helenus-signal-lifecycle` (trap: "Sq9
  weights, zone min_score=35"), `helenus-failure-archaeology/reference.md` (§8).
- Issue: when the owner finalizes weights, four files go stale simultaneously; a
  session loading only signal-lifecycle would still treat finalized values as
  provisional (or vice versa). change-control already carries the right hedge
  ("re-read helenus/config.py rather than trusting the table") — the others don't.
- Fix: campaign V0 is the home for the concrete values (it already mandates a config
  re-check). Siblings should say "provisional as of 2026-07-05 — verify in
  helenus/config.py" without restating numbers.

**F-10 — docs-and-writing trap #2 is a misleading sentence about a cost-critical
rule.**
- File: `helenus-docs-and-writing/SKILL.md` §8, bullet 2: "Editing the lessons block
  wording in a way that invalidates prompt-cache assumptions is fine (lessons load
  AFTER the cached block)…".
- Issue: as written, it asserts that *invalidating prompt-cache assumptions is fine* —
  the opposite of analyst-prompting's core rule. The intended meaning (editing lessons
  is safe BECAUSE it can't invalidate the cache) only emerges on careful re-read. A
  skimming model could take away "cache invalidation is fine".
- Fix: rewrite: "Editing lessons.md wording is cache-safe (lessons load AFTER the
  cached block); moving lessons INTO the cached methodology block is not — see
  helenus-analyst-prompting."

**F-11 — Campaign Phase V1 (back-tag) is not executable without guessing — and it is
the library's centerpiece.**
- File: `helenus-confluence-validation-campaign/SKILL.md` §5 V1;
  `helenus-diagnostics-tooling/SKILL.md` Known uncertainty.
- Issue: V1 says "Recompute per alert from its timestamp + entry price against the
  session tape" and "pull more days via `scripts/replay_today.py` equivalents if
  Schwab history allows". There is no command, no engine entry-point named (how do I
  call `sq9.assess()` / the zone engine against a historical bar window?), no code
  sketch, and diagnostics-tooling separately admits it's unverified whether
  replay_today can pull past dates at all, and that the jsonl outcome-linkage field
  name is unverified. `_expert_distillation_notes.md` §5 confirms no back-tag script
  exists. A junior engineer or Sonnet session hits Phase V1 and must invent the whole
  harness — the highest-guessing step in the highest-stakes skill. Everything else in
  the library is copy-pasteable; this isn't.
- Fix: add to V1 a minimal recipe: the engine entry points with signatures (e.g.
  "instantiate Sq9 from `Sq9Config`, call `assess(price, ...)` per bar — read the
  docstring contract in `helenus/engine/sq9.py` first"), the jsonl fields that give
  timestamp/entry (with the verify-in-journal.py caveat), and an explicit first step
  "verify whether replay_today accepts a past date; if not, coverage = tape_2026-06-22
  only, and the report must say so."

### MINOR

**F-12 — Four eval files are slightly under the one-third negative guideline.**
- Files: diagnostics-tooling, docs-and-writing, market-domain-reference,
  validation-and-qa evals (6/21 = 28.6% negative).
- Fix: swap one positive for a hard negative each (e.g. market-domain: "explain gamma
  scalping for my own portfolio").

**F-13 — 26 vs 29 graded-alert counts sit unexplained next to each other.**
- Files: `helenus-failure-archaeology/SKILL.md` §9 ("doctrine from 26 graded alerts
  (34.6% accurate baseline)") vs "29 graded alerts" in research-discipline, campaign,
  and the capability map.
- Issue: both are correct (lessons.md was curated at n=26; the journal now holds 29)
  but nothing says so in the SKILL.md, and a careful zero-context reader will flag it
  as a contradiction. reference.md explains it; the SKILL.md doesn't.
- Fix: one parenthetical in archaeology §9: "(lessons.md curated at n=26; journal now
  29 — see reference.md)".

**F-14 — Header-numbering inconsistency.**
- Files: debugging-playbook, research-discipline (actually numbered ok), validation-and-qa,
  failure-archaeology use unnumbered "## Provenance and maintenance" while most files
  number it "## 10."; debugging-playbook's sections shift (Procedure is §6 there, §5
  elsewhere). Cosmetic only; harmless to models, mildly annoying to maintainers.
- Fix: normalize, or don't — no behavioral risk.

**F-15 — Campaign's highest-value trigger phrases sit at the end of its description.**
- File: `helenus-confluence-validation-campaign/SKILL.md` frontmatter.
- Issue: "continue the rework", "finalize weights" — the verbatim user phrases — come
  after two clauses of framing. Most other skills lead with trigger vocabulary. Low
  risk (the terms are present), but if F-02 is fixed the description gets rewritten
  anyway — front-load the quoted phrases then.

**F-16 — Deliberate repetition that is fine as-is (recorded so no one "fixes" it).**
- "Journal is append-only" (6+ skills), "the veto test" name-checks (4 skills),
  VIX1D-distortion one-liners (4 skills, each pointing at FlowConfig), "~dozens of
  Claude calls/session" (4 skills). These are one-line safety rules whose homes
  (docs-and-writing, research-discipline, market-domain-reference,
  architecture-contract respectively) carry the detail; the copies are pointers or
  single sentences with low drift surface. Acceptable redundancy for safety-critical
  rules — leave them.

---

## 3. Checks that passed (for the record)

- **Trigger quality overall:** descriptions are distinctive, lead with concrete
  vocabulary, and encode false-positive guards ("Owner-flagged: …#1 weaker-model
  failure", "NOT pytest", symptom lists). Apart from F-01/F-02/F-04/F-05, I could not
  construct a should_trigger query that a sibling's description would steal, and the
  should_not negatives (react apps, cover letters, Roman empire, generic finance
  homework) would clearly stay quiet.
- **Matrix conformance:** matrix rules 1–5 are faithfully reflected in the JSON
  routings (spot-checked every cross-routed negative: won't-run→build-test-env,
  assertion→validation-and-qa, live→debugging, was-it-tried→archaeology,
  does-it-work→discipline, rework→campaign, WHY→domain / WHERE→orientation /
  MAY-NEVER→contract). Rule 6 is the only one with cracks (F-04).
- **Size/scannability:** all SKILL.md files are 76–104 lines — well under the 150
  bar — with a consistent 10-section template, tables instead of prose, and the long
  material genuinely pushed to reference.md (archaeology stories, dealer-mechanics
  chains). No wall-of-text sections. Each skill's single most important rule is
  surfaced in Purpose or the description itself (campaign: "the deliverable is a
  report, not a weight change"; change-control: the four rules; validation-and-qa:
  never rewrite a hand-computed fixture).
- **Actionability:** every skill has a "When not to use" with sibling pointers, a
  named output artifact, copy-pasteable commands where relevant (both bash AND
  PowerShell in build-test-env — correct for this Windows repo), and a concrete
  "bad behavior this prevents" vignette. The vignettes are the best feature of the
  library: each one is a realistic session-level failure a weaker model would commit.
- **Coverage:** the 15 skills cover the capability map's §10 list one-for-one. Real
  gaps are small: no procedural home for data-layer changes (schwab_feed/stream
  internals) or for adding a `!command` to bot.py — both currently served indirectly
  by architecture-contract + change-control, which is defensible for a v1.

---

## 4. Overall verdict

This is a genuinely usable library — well above the bar for zero-context transfer.
Sizes are disciplined, every skill is procedural rather than essayistic, the
negative-space ("when NOT to use", false friends, fenced-off wrong paths) is the
strongest I have seen in a skill set of this kind, and all 315 eval cases parse and
are realistically phrased. A Sonnet-class session landing on any single skill would
execute correctly in almost every scenario the model-transfer benchmark poses. The
one BLOCKING item is narrow but real: the library's designated safety net for
auto-execution pitches cannot fire because its trigger description never mentions
them — a one-line description fix. The IMPORTANT items cluster into three themes:
trigger over/under-claiming around the campaign and the Schwab-401 boundary
(F-02/F-04/F-05/F-06), single-home-per-fact drift risk on the environment facts and
provisional config values (F-08/F-09, plus the twin "tier" tables in F-07 which is
the most conceptually dangerous duplication), and the executability hole at campaign
Phase V1 (F-11) — which deserves priority because it sits at the exact center of the
work the owner says matters most. Fix F-01, F-07, and F-11 before relying on the
library for the validation campaign; the rest can be batched into a maintenance pass.
