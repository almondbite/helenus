# Trigger matrix (Phase 4)

How the 15 skills partition the query space. Manual invocation: mention the skill name
or ask Claude to "use the <name> skill"; each also has `evals/trigger_eval.json` with
20+ realistic queries (typos and vagueness included) for regression-testing triggering.

Golden disambiguation rules (sibling conflicts, in priority order):
1. **Doing the rework validation** → `confluence-validation-campaign` beats
   `research-discipline` (which defines the bar) and `signal-lifecycle` (which defines
   the generic ladder). Campaign = the concrete current work.
2. **"Does X work / is X validated?"** → `research-discipline`. **"Was X already
   tried/ruled on?"** → `failure-archaeology`. **"How do I take X live?"** →
   `signal-lifecycle`.
3. **Test won't RUN** → `build-test-env`. **Test FAILS with an assertion** →
   `validation-and-qa`. **Live behavior wrong** → `debugging-playbook`.
4. **Any mutation intent** ("change/enable/commit/fix while here") layers
   `change-control` on top of whatever domain skill applies.
5. **WHY the market does X** → `market-domain-reference`. **WHERE the code does X** →
   `repo-orientation`. **WHAT the code may never do** → `architecture-contract`.
6. **Prompt/cost/Signal-schema** → `analyst-prompting` (repo-specific) vs the global
   `claude-api` skill (API-generic).
7. **Auth/credential errors AT STARTUP** (401, token expiry, OAuth) →
   `build-test-env`; **mid-session data staleness/misbehavior** → `debugging-playbook`.
8. **The validation report:** `confluence-validation-campaign` owns its CONTENT
   (Phase V4); `docs-and-writing` owns format/templates
   (`templates/validation_report.md`). "Write the report" during the campaign →
   campaign skill, which references the template.

| Skill | Should trigger (examples) | Should NOT trigger (route to) | False-positive guard in description |
|---|---|---|---|
| repo-orientation | "what is this project", "where does the gate live", "tour before we start" | run tests (build-test-env); why market does X (domain-ref) | scoped to orientation/navigation/trust questions |
| change-control | "just enable the flag", "small change to a default", "also fix the README while there" | pure reads; generic refactors outside repo | keyed to mutation/status-language intent |
| build-test-env | "ModuleNotFoundError: helenus", "no module named pytest", "fresh machine setup" | assertion failures (validation-and-qa) | keyed to run/setup/env errors |
| debugging-playbook | "no alerts fired", "stream stale", "schwab 400", "times off by hours" | test failures; edge questions | keyed to live/replay misbehavior symptoms |
| failure-archaeology | "add an ES veto", "was X tried", "evidence behind the 8pt rule" | fresh evidence design (research-discipline) | keyed to precedent/history/re-proposal |
| architecture-contract | "make it async", "let zones veto", "reorder precedence" | mechanics of market (domain-ref) | keyed to design/review/bypass proposals |
| research-discipline | "it's validated", "62% win rate", "threshold shopping" | executing the campaign (campaign skill) | keyed to claim-evaluation verbs |
| signal-lifecycle | "new trigger idea", "promote/retire X" | evaluating evidence per se; current rework | keyed to proposals + promotion ladder |
| confluence-validation-campaign | "continue the rework", "min_score?", "finalize weights" | brand-new signals (lifecycle) | names the concrete rework artifacts |
| market-domain-reference | "gex sign", "why vix1d", "quote rule" | board looks wrong (debugging) | keyed to WHY/mechanics, warns it's Helenus-applied not generic finance tutoring |
| diagnostics-tooling | "what would the gate have fired", "grade today", "query the jsonl" | interpreting significance (research-discipline) | keyed to instruments + artifact locations |
| validation-and-qa | "assertion failed", "update the fixture?", "legacy_gex?" | can't import (build-test-env) | keyed to tests-as-evidence + fixture rules |
| docs-and-writing | "update lessons.md", "can I say verified", "clean up docs" | whether evidence supports it (research-discipline) | keyed to writing/status-language acts |
| research-frontier | "what next", "ML on the journal", "roadmap" | current mandated work (campaign) | keyed to open problems/ambition; auto-exec pitches route here to hit the anti-goals list |
| analyst-prompting | "_SEC_ sections", "cache_control", "Signal schema", "API cost spike" | generic Claude API (claude-api skill) | keyed to analyst.py specifics |

Notes:
- `research-frontier` deliberately CATCHES dangerous ambitious pitches (auto-execution,
  ML-on-29-alerts) so its anti-goals section fires before code gets written.
- `change-control` and a domain skill often co-trigger by design (rule 4): the domain
  skill answers, change-control gates.
- Eval-format note: each JSON's `expected_skill` on `should_trigger:false` rows names
  the sibling that SHOULD handle it (or null for out-of-scope), which doubles as a
  routing regression test.
