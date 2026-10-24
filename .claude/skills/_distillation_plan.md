# Helenus capability-distillation plan (Phase 0)

Date: 2026-07-05. Author: Claude Fable 5 distillation session.

**Scope note (important):** the mission brief was written against a "Goblin" Forex/MT5
template. This repository is **Helenus** — an SPX/SPY 0DTE dealer-positioning bot
(Schwab API → local GEX/charm/flow/tape math → mechanical candidate gate → Claude
analyst verdict → Discord embeds → graded journal → lessons feedback loop). Per the
brief's own instruction ("do not force this exact taxonomy if the repo shows a better
one"), all Goblin-specific skill families are translated to their Helenus analogs:

| Goblin concept | Helenus analog |
|---|---|
| Python green ≠ MT5 truth | Unit-fixture green ≠ live-tape truth; replay ≠ live session |
| Screen ≠ candidate | Flag-gated engine ≠ validated confluence; enabled ≠ weighted |
| Gross ≠ net | Raw MFE ≠ tradeable edge (spread, premium bleed, early reversal) |
| Hypothesis ≠ result | lessons.md entry without cited alert IDs = claim, not lesson |
| MT5 parity gates | Poll-path vs stream-path parity; legacy-flag vs new-flag parity |
| Component contracts | Flag-gated, default-controlled engines with `degraded` disclosure |
| Sealed holdout | Small-n journal (29 graded alerts) — no weight finalization before back-tag validation |

## Intended future users
1. GPT 5.5 / Opus 4.8 / Sonnet-class coding sessions with zero conversation context.
2. Junior/mid-level engineers new to options market structure and this codebase.
3. The project owner reviewing high-stakes signal-weighting and go-live decisions.

## Capability types to preserve
- Repo navigation (what is where, what is stale, what is generated).
- Architecture reasoning (pure-math engines, flag gates, gate precedence, prompt assembly).
- Debugging (Schwab API quirks, stream staleness, timezone/session logic, Discord loop).
- Test/build execution (global Python, no pytest, standalone runners, PYTHONPATH).
- Quant research discipline (journal-graded evidence bar, small-n humility, no production tuning).
- Signal-lifecycle governance (idea → flag-gated build → hand fixtures → replay → live grading → lessons → weight decision).
- Evidence review (what a green test proves; what a 29-alert journal cannot prove).
- Forensic failure archaeology (the ES-imbalance demotion saga, the 7340–7350 absorption cluster, dead fields like `vanna_active`).
- Documentation maintenance (README drift, lessons.md house style, append-only journal).
- Skeptic/adversarial review (red-flag phrases, decision trees).
- Promotion/rejection decisions (the confluence-rework VALIDATION phase protocol).

## Failure modes the skills must prevent
- Hallucinated paths, flags, commands, or config fields (e.g. inventing a `pytest.ini`, a `.venv`, or a CI pipeline — none exist).
- Treating a passing hand-fixture suite as proof a signal works on live tape.
- Treating replay output (`scripts/replay_gate.py`) as live behavior (replay has no volume, no flow, no GEX unless supplied).
- Treating 29 graded alerts as statistically sufficient to finalize confluence weights.
- Tuning gate thresholds "in production" instead of against the journal/replay (README's explicit rule).
- Enabling default-off flags (`GexMapConfig.enabled`, `CumDeltaConfig.enabled`, `ApproachConfig.enabled`, `ESLeadConfig.enabled`, `FlowConfig.inflection_enabled`) as a "fix".
- Letting Sq9 initiate or veto (its contract forbids both), or putting zone edges / Sq9 rings into scan2's key-level grid.
- Overwriting `journal/helenus.jsonl` or rewriting `lessons.md` history instead of appending dated corrections.
- Trusting the README's architecture prose over the working tree (README predates the 2026-07-04/05 confluence rework; its Setup section says `.venv` but the real environment is global Python 3.12, no pytest).
- "Claude invented a new strategy" instead of following the governed one-task-at-a-time, plan-first, flag-gated, hand-fixture-tested workflow the owner mandated.
- Saying fixed/verified/done without a command, artifact, or cited alert ID.

## Expected final artifact inventory
- `_distillation_plan.md` (this file), `_repo_capability_map.md`, `_expert_distillation_notes.md`.
- ~15 skill folders, each `SKILL.md` + `evals/trigger_eval.json`; `reference.md`/`templates/` where warranted.
- `_trigger_matrix.md`, `_model_transfer_eval.md`.
- `_review_factual.md`, `_review_doctrine.md`, `_review_usability.md`, `_fixer_report.md`.
- `README.md` (index), `_uncertainty_register.md`, `_maintenance_plan.md`.

## Ground rules for authoring
- Write only under `.claude/skills/`. No mutating git commands. No repo edits.
- Every command/path/flag verified against the working tree before being written, or labeled `unverified`.
- Status vocabulary: known / observed / candidate / unverified / stale-risk / owner-confirmation-needed.
- Repo-relative paths only; machine-specific paths only as clearly-labeled examples.
