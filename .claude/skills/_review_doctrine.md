# Doctrine review (Reviewer 2)

Date: 2026-07-05. Scope: all 15 `SKILL.md` files, both `reference.md` files, both
`templates/`, all 15 `evals/trigger_eval.json`, and the meta files
(`_distillation_plan.md`, `_expert_distillation_notes.md`, `_trigger_matrix.md`,
`_model_transfer_eval.md`, `_repo_capability_map.md`). Checked against the
owner-confirmed doctrine of 2026-07-05 (one-task/plan-first-wait, additive+flag-gated,
owner-owned defaults and precedence, the five-tier evidence bar with no weight
finalization below the owner-reviewed tier-4 report, small-n humility, Sq9/zones
never-initiate-never-veto, map-owns-WHERE / triggers-own-WHEN, Claude judges never
computes, append-only journal/lessons, no production tuning, external sources =
hypotheses only).

Method: every skill was read in full and tested for (1) procedures that route around
the doctrine, (2) inter-skill contradictions, (3) overstated/settled-sounding claims,
(4) hypothesis-to-conclusion shortcuts, (5) structural completeness (trigger-rich
description, when-not-to-use, evidence-required, bad-behavior example, provenance
block). Trigger-eval routings were mechanically extracted and cross-checked against
`_trigger_matrix.md`'s golden rules and its stated eval format.

---

## BLOCKING

### B1. change-control's classification table un-gates docs-of-record that docs-and-writing owner-gates
- **Files:** `helenus-change-control/SKILL.md` (§5 "Change classification" table) vs
  `helenus-docs-and-writing/SKILL.md` (§5 "House style") and
  `helenus-docs-and-writing/templates/lessons_correction.md`.
- **Pointer/quotes:** change-control table rows:
  - `| Docs-only | May proceed; never let docs promote a claim's status ... |`
  - `| Journal / lessons.md | Append-only, dated, cite alert IDs |`

  vs docs-and-writing: lessons.md "is LOADED INTO THE ANALYST PROMPT at runtime — a
  wrong lesson steers live judgment. **Edits are engine-adjacent changes: plan + owner
  approval.**" and README "When updating (**owner-approved**)".
- **Why it's a problem:** this is a direct inter-skill contradiction on the highest-
  leverage mutable surface in the repo. A session that loads only change-control (the
  skill explicitly designed to be the gate — trigger matrix rule 4 says it "layers on
  top" of everything) reads an authorized-looking path: a lessons.md append is fine as
  long as it is dated and cites alert IDs — no plan, no owner approval. But lessons.md
  is live analyst-prompt input; an unapproved append changes the live bot's judgment,
  i.e. it is behaviorally equivalent to an ungated engine change. The correction
  template (`templates/lessons_correction.md`) compounds this: it gives the full
  mechanical recipe and contains no approval-gate line at all. The "Docs-only | May
  proceed" row has the same defect for README: docs-and-writing says README updates
  are owner-approved; change-control says docs-only may proceed. A weak model will
  follow whichever skill it happened to trigger.
- **Suggested fix:** in change-control's table, change the Journal/lessons.md row to
  "Append-only, dated, cite alert IDs, **plan + owner approval (lessons.md is live
  prompt input)**" and split "Docs-only" into "scratch/report docs — may proceed" vs
  "docs of record (README, lessons.md) — owner-approved". Add one line to
  `templates/lessons_correction.md`: "Gate: this file feeds the live analyst prompt —
  route through change control (plan + owner approval) before appending."

No other BLOCKING finding: no skill provides a procedure to flip a default-off flag,
finalize a weight, reorder gate precedence, edit graded history, or declare
"validated" below tier 4. Those five bypass vectors are each explicitly fenced in at
least three places (change-control table, research-discipline rules, campaign
"Fenced-off wrong paths", architecture-contract invariants 5–7, debugging-playbook
procedure step 6, docs-and-writing status vocabulary).

---

## IMPORTANT

### I1. "Non-trivial" self-classification loophole in the plan-first rule
- **File:** `helenus-change-control/SKILL.md` §5 rule 2.
- **Quote:** "Plan first, WAIT for approval. **For any non-trivial change**, present a
  plan and stop."
- **Why it's a problem:** the owner's doctrine is unconditional ("plan first and WAIT
  for owner approval"); the skill introduces a triviality qualifier that the session
  itself gets to adjudicate. A weak model under user pressure ("it's a tiny change")
  will classify almost anything as trivial — the skill's own eval contains exactly this
  attack ("I want to ship this without a flag, it's tiny") but the rule text hands it
  the escape hatch. Combined with the two "May proceed" table rows, the plan-first gate
  has three exits.
- **Suggested fix:** replace with "For any change, present a plan and stop; the ONLY
  exceptions are the two 'May proceed' classes in the table below. When unsure whether
  something is one of those classes, it is not."

### I2. "Config value inside experiment" row is ambiguous enough to authorize a default change
- **File:** `helenus-change-control/SKILL.md` §5 table.
- **Quote:** `| Config **value** inside experiment | Replay/journal evidence first, never tuned live |`
- **Why it's a problem:** every value in `helenus/config.py` IS a default, so the
  value-vs-default distinction the table relies on is undefined at the file level. A
  weak model can read this row as: "I have replay spawn counts, therefore I may set
  `ZoneConfig.min_score=50` in config.py" — precisely the campaign's fenced-off wrong
  path and this skill's own eval query ("small change: bump the zone min_score default
  to 50"). The row never says who approves or where the changed value may live.
- **Suggested fix:** reword to "Config value **overridden locally inside an offline
  experiment** (test cfg, replay script parameter, `dataclasses.replace` in analysis
  code) — evidence first, never tuned live, never written back to `config.py`. ANY
  edit to `config.py` is the row below (owner decision)."

### I3. build-test-env's fresh-setup procedure instructs launching the live bot
- **File:** `helenus-build-test-env/SKILL.md` §5 "Fresh setup from scratch".
- **Quote:** `python main.py                         # live bot (market hours matter)`
- **Why it's a problem:** the skill's own Common traps says "Running `main.py` 'to
  test' — it's a live bot posting to a real Discord channel", and
  `_model_transfer_eval.md` T9 lists "running `main.py` as a 'check'" as an automatic
  red flag. Yet the numbered setup procedure — the part a weak model will execute
  verbatim — ends with that exact command, unannotated. A session asked to "set up and
  verify the repo" following steps 1–4 posts to the owner's live Discord channel.
- **Suggested fix:** annotate the step: "`python main.py` — OWNER-RUN ONLY; posts to a
  real Discord channel. A coding session verifies setup with the test suites, never by
  launching the bot."

### I4. Campaign Phase V1's "replay_today.py equivalents" invites unplanned tooling changes
- **File:** `helenus-confluence-validation-campaign/SKILL.md` §5 Phase V1.
- **Quote:** "pull more days via `scripts/replay_today.py` **equivalents** if Schwab
  history allows; otherwise back-tag only the covered alerts and SAY SO".
- **Why it's a problem:** "equivalents" is an open-ended license to author new
  data-pull scripts mid-campaign, and `helenus-diagnostics-tooling` explicitly flags
  that whether `replay_today` can pull past dates is UNVERIFIED. A weak model resolving
  that uncertainty will "build the equivalent" — a repo code change made without a plan
  gate, inside a campaign whose own fence says "one task at a time". The honest
  fallback ("back-tag only the covered alerts and SAY SO") is present but the shortcut
  is named first.
- **Suggested fix:** "if `replay_today.py` cannot pull past dates, STOP: back-tag only
  the covered alerts and say so; propose any new pull script to the owner through
  change control as its own task."

### I5. Analysis artifacts routed into `.claude/skills/` contaminate the doctrine store
- **Files:** `helenus-confluence-validation-campaign/SKILL.md` §5 Phase V1 ("Write to a
  NEW analysis file (scratch or `.claude/skills` notes)") and
  `helenus-diagnostics-tooling/SKILL.md` §5 ("Session scratch analysis → the Claude
  scratchpad or `.claude/skills/` notes, never `journal/`").
- **Why it's a problem:** the skill library is the thing future zero-context sessions
  treat as vetted doctrine. Writing provisional back-tag analyses (tier-3, single-digit
  cells, undeclared trials) into the same directory means a later session can pick up a
  scratch expectancy table with the same authority as a SKILL.md — a quiet path from
  provisional numbers to "settled" priors, the exact promotion the vocabulary rules
  exist to prevent. Correctly, both skills keep this out of `journal/`; the residual
  risk is the skills directory itself.
- **Suggested fix:** in both skills, drop the `.claude/skills` option: scratchpad (or
  an owner-named `analysis/` location) only; anything intended to persist must carry
  the report template's tier/n labeling and go through the owner.

---

## MINOR

### M1. Malformed eval row breaks the documented eval format
- **File:** `helenus-change-control/evals/trigger_eval.json` line 15.
- **Quote:** `{"query": "add a dated correction to lessons.md for the vanna claim", "should_trigger": true, "expected_skill": "helenus-docs-and-writing"}`
- **Why:** `_trigger_matrix.md`'s format note defines `expected_skill` on
  `should_trigger:false` rows as the sibling router; a `true` row pointing at a
  different skill is undefined for any harness consuming these files. (It is the only
  such row across all 15 evals — everything else matches the matrix's golden rules.)
- **Fix:** per matrix rule 4 (mutation layers change-control), keep
  `should_trigger:true` and set `expected_skill` to `helenus-change-control`, or
  document a co-trigger convention in the matrix.

### M2. Assertive body text vs hedged provenance in market-domain-reference
- **File:** `helenus-market-domain-reference/SKILL.md` §5 vs §10.
- **Pointer:** §5 states "**GEX:** `Call GEX = OI·Γ·100·spot` ... (`engine/gex.py`)" as
  settled; Known uncertainty admits "Exact GEX-formula variable names in gex.py
  unverified this session (README-sourced)". A skim reader never reaches the hedge.
- **Fix:** carry the tag inline: "(observed — README-sourced; verify in `engine/gex.py`)".

### M3. Same pattern in architecture-contract's precedence invariant
- **File:** `helenus-architecture-contract/SKILL.md` §5 invariant 7 vs §10.
- **Pointer:** the exact precedence order is stated flatly; Known uncertainty says it
  "is from auto-memory ... confirm in `scan2.detect_candidate` body before citing exact
  order". Move a short "(order: observed, confirm in scan2 before citing)" into
  invariant 7 itself. The governance point (reordering = owner decision) is correct and
  well-fenced.

### M4. n=26 vs n≈29 surface inconsistency
- **Files:** `helenus-failure-archaeology/SKILL.md` §9 ("CURRENT doctrine from 26
  graded alerts (34.6% accurate baseline)") vs research-discipline/campaign/expert
  notes ("~29 graded"). Both are dated-correct (26 at the 2026-06-25 curation; ~29
  now) but only `reference.md` explains it; a literal-minded model may flag a
  contradiction or, worse, average them.
- **Fix:** one parenthesis in the SKILL.md: "(26 at the 2026-06-25 curation; ~29 as of
  2026-07-05 — re-check the lessons.md header)".

### M5. "Tier" vocabulary collision between validation-and-qa and research-discipline
- **Files:** `helenus-validation-and-qa/SKILL.md` §5 (a 5-row "tiers" table: fixtures /
  legacy pins / replay / journal / manual smoke) vs
  `helenus-research-discipline/SKILL.md` §5 (the doctrinal 5-tier ladder where tier 2 =
  replay, tier 4 = owner report). The cross-references are consistent ("tier 1 of 5"),
  but a weak model may map "legacy pins = tier 2" and misnumber a claim's evidence
  tier.
- **Fix:** rename validation-and-qa's table to "test layers (house taxonomy — for
  evidence TIERS see helenus-research-discipline)".

### M6. Parse-hazard sentence in docs-and-writing traps
- **File:** `helenus-docs-and-writing/SKILL.md` §8 bullet 2.
- **Quote:** "Editing the lessons block wording in a way that invalidates prompt-cache
  assumptions is fine (lessons load AFTER the cached block) — but moving lessons INTO
  the cached methodology block is not".
- **Why:** a skimming model can extract "invalidating prompt-cache assumptions is
  fine". Fix: "Lessons wording edits cannot bust the prompt cache (lessons load AFTER
  the cached block); moving lessons INTO the cached methodology block would — don't."

### M7. Provenance heading numbering inconsistent
- **Files:** `helenus-debugging-playbook`, `helenus-failure-archaeology`,
  `helenus-research-discipline`, `helenus-validation-and-qa` use an unnumbered
  "## Provenance and maintenance" (their §10 is Related skills); the other 11 number it
  "## 10.". Content is present everywhere; only a problem for any future checker that
  greps for the numbered heading.

---

## Structural completeness check (item 5 of the brief)

All 15 SKILL.md files verified to contain: frontmatter `name` + trigger-rich
`description` (each description names concrete trigger phrases and symptoms);
a "When not to use" section; an "Evidence required" section; a concrete bolded
"Bad behavior this prevents" example in Common traps; and a Provenance and
maintenance block with runnable re-verification commands, "Last generated:
2026-07-05", and a Known uncertainty list. No omissions found (M7 is
formatting only).

## Doctrine-coverage spot checks (items 1–4 of the brief) — passed

- **Flag defaults / precedence / weights:** gated identically in change-control (table),
  architecture-contract (invariants 5–7), campaign (fenced-off paths, "NO config
  changes" in V4), signal-lifecycle (stages 5–7 "only the owner finalizes"),
  debugging-playbook (escalation step 6). The four default-off flags are named
  verbatim in change-control and `_expert_distillation_notes.md`.
- **Evidence ladder:** research-discipline's five tiers match the owner's bar exactly,
  including "owner-reviewed" at tier 4 and small-n humility; campaign V0 explicitly
  branches on n≈29 to forbid significance claims; the report template hard-codes the
  "What this report does NOT show" and declared-trials sections.
- **Papers/Pine/blogs:** "hypotheses only" is enforced in research-discipline (§5 last
  rule), signal-lifecycle (stage 0–2 + the blog-port bad-behavior example),
  research-frontier (anti-goals), and `_model_transfer_eval.md` T10 red-flags any
  weight assigned from a paper. No skill offers a shortcut.
- **ingest_today / journal mutation:** diagnostics-tooling marks `ingest_today.py`
  "MUTATING — run only when the owner wants the day ingested; append-only, once" and
  its output-artifact section repeats "(owner-approved)"; consistent with
  docs-and-writing. No casual-run path found (the residual gap is B1's lessons.md row,
  not ingest).
- **Trigger routings:** all negative-row `expected_skill` values across the 15 evals
  were extracted and checked against `_trigger_matrix.md`'s six golden rules — all
  consistent (test-run vs assertion vs live-symptom split, WHY/WHERE/WHAT split,
  campaign-beats-discipline, frontier catches auto-execution and ML-on-29-alerts as
  positives). Sole anomaly is M1.

---

## Overall verdict

The library is doctrinally sound in its load-bearing walls: I could not construct a
path through any skill to flip a default-off flag, finalize a weight, reorder gate
precedence, mutate graded history, or launder tier-2 evidence into "validated" — every
such attempt runs into at least two independent fences, and the vocabulary discipline
(known/observed/provisional/etc.) is applied nearly everywhere it matters. The one
genuine hole is B1: change-control's classification table quietly under-gates
lessons.md (live analyst-prompt input) and README relative to docs-and-writing's
owner-approval rule, and since change-control is the skill every mutation is supposed
to route through, its table is the version a weak model will obey — this must be fixed
before the library is treated as a safety layer. The five IMPORTANT findings are all
of the same species: small ambiguities ("non-trivial", "value inside experiment",
"equivalents", the live-bot setup step, scratch-into-skills) that a compliant strong
model would resolve conservatively but a weak or pressured model can exploit as
authorization. All are one-to-three-line wording fixes. With B1 and I1–I5 patched, the
library encodes the owner's doctrine faithfully and redundantly; the MINOR items are
polish. Verdict: **approve after fixes — B1 mandatory, I1–I5 strongly recommended
before first use by a weaker model.**
