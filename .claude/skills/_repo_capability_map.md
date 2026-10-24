# Helenus repo capability map (Phase 1)

Generated 2026-07-05 by direct inspection. Status vocabulary: **verified** = command run
or file read this session; **observed** = read in repo docs/memory but not executed;
**stale-risk** = documented but contradicted by newer evidence.

## 1. Project purpose
Helenus is an SPX/SPY 0DTE options **structure-reading bot**. It pulls the PM-settled
SPXW 0DTE chain from the Charles Schwab API, computes dealer gamma exposure (GEX),
charm, and options-flow structure locally with deterministic Pandas/math, runs a cheap
mechanical **candidate gate** over a rolling 1-minute tape, and only when the gate fires
spends a **Claude (Opus-class) analyst call** that renders a typed verdict
(`has_signal/direction/confidence/thesis/risk_flags`) posted as a Discord embed. Every
intraday alert is graded deterministically (MFE/MAE over a 30-bar window) into an
append-only JSONL journal; Claude reviews graded history into `journal/lessons.md`,
which feeds back into the analyst's system prompt. It is an alerting/judgment tool, not
an auto-executing trader. (verified: README.md + code)

## 2. Main subsystems (verified against working tree)
- `helenus/config.py` — all tuning as frozen dataclasses; one global `CONFIG = HelenusConfig()`. 21 config classes incl. new `ConvictionConfig`, `ZoneConfig`, `Sq9Config`.
- `helenus/data/schwab_feed.py` — REST chain/quote polling, `AdaptiveThrottle`, OAuth token.
- `helenus/data/schwab_stream.py` — flag-gated websocket: option premium ticks, /ES level-one, SPY 1m bars.
- `helenus/engine/` — pure-math engines (no I/O except `analyst.py`):
  `gex.py`, `gexmap.py` (persistent map + directional prior, flag-gated), `charm.py`,
  `flow.py` (vanna tracker), `scalp.py` (EMA ignition; 5m/ITM confluence mode),
  `displacement.py`, `conviction.py` (Pine oscillator port, NEW/untracked),
  `zones.py` (Pine zone-lifecycle port, NEW/untracked), `sq9.py` (Gann lattice,
  NEW/untracked), `orb.py`, `moc.py`, `cumdelta.py`, `es_lead.py`, `intermarket.py`,
  `scan2.py` (tape + key levels + `detect_candidate` gate + `detect_approach`),
  `analyst.py` (the only network engine: assembles `_SEC_*` prompt sections → Claude).
- `helenus/bot.py` — discord.py event loop, worker topology, `!` command handlers
  (`!gex !sq9 !conv !zones !charm !scalp !disp !orb !moc !stream !scan !flow !inter !stats !review`).
- `helenus/journal.py` — MFE/MAE grading + append-only JSONL.
- `helenus/output/embeds.py` — Discord embed builders.
- `scripts/` — OAuth (`authorize.py`), offline retrospective (`replay_today.py`,
  `grade_today.py`, `replay_gate.py`, `ingest_today.py`), ops helpers
  (`resend_alerts.py`, `send_test_alert.py`, `check_channel.py`, `check_history.py`,
  `diagnose_channel.py`, `test_commands_bot.py`).
- `tests/` — 22 standalone hand-fixture suites (also pytest-discoverable, but pytest is NOT installed).
- `pine_files/` — untracked Pine source (`Displacement_Oscillator`, `Displacements_Atlas`) the conviction/zones ports were made from.
- `journal/` — `helenus.jsonl` (29 alerts / 29 outcomes / 6 reviews — verified by parse), `lessons.md` (doc of record for empirical priors), `tape_2026-06-22.json` (real-tape fixture).

## 3. Build/test/run commands (verified 2026-07-05)
- Python: **global 3.12.10** at the user level. **No `.venv` exists** (README's Setup
  section describing `python -m venv .venv` is stale-risk — deps are installed globally).
- **pytest is NOT installed** (`python -m pytest --version` → "No module named pytest").
  Run suites standalone; they need the repo root on `PYTHONPATH`:
  - Bash: `PYTHONPATH=. python tests/test_gex.py`
  - PowerShell: `$env:PYTHONPATH='.'; python tests\test_gex.py`
  - Verified green this session: `test_gex.py` (10 passed), `test_scan2.py` (26 passed), `test_conviction.py` (27 passed).
- Run the bot: `python main.py` (needs `.env` secrets + `schwab_token.json`; NOT run this session — live network).
- One-time OAuth: `python scripts/authorize.py` (observed, interactive browser flow).
- No CI configuration exists (no `.github/`, no pipeline files). No linter/type-checker config.

## 4. Research workflows (observed in README + scripts, replay path exercised by prior sessions)
1. `scripts/replay_today.py` → pulls today's real 1m tape → `journal/tape_<date>.json`.
2. `scripts/replay_gate.py` → feeds a real tape through `detect_candidate` offline — the
   threshold-tuning harness ("tune against this, not in production").
3. `scripts/grade_today.py` → grades the day's journaled alerts against the real forward tape.
4. `scripts/ingest_today.py` → appends `outcome` records + a session `review` and updates `lessons.md` — closes the loop offline.
5. Live loop: alert → 30-bar MFE/MAE grade → `!stats` (deterministic) / `!review` (Claude pattern review, also auto at 16:00 ET) → `lessons.md` → analyst prompt priors.

## 5. Streaming/parity workflows (the repo's "MT5 parity" analog)
- Poll path is the always-on baseline; the stream (`StreamConfig.enabled`, default on) is
  additive with a staleness gate — every consumer must no-op or fall back when a stream is stale.
- Flag parity discipline (verified in tests + memory): every new capability ships
  **flag-gated with the legacy behavior exactly preserved when off**, and legacy tests are
  pinned via context managers (`gex_mode()`/`gexmap_enabled()` in
  `tests/test_gex_decouple.py`; `legacy_gex()` in `tests/test_scan2.py`/`test_approach.py`/
  `test_es_lead.py`; `legacy_scalp()` in `tests/test_scan2.py`; explicit LEGACY cfg in
  `tests/test_scalp.py`).

## 6. Critical docs of record
- `README.md` — architecture narrative + design notes + "Known structural placeholders". **Stale-risk**: predates the 2026-07-04/05 confluence rework (Tasks 1–4) and describes the pre-rework gate stack; Setup section describes a venv that doesn't exist.
- `journal/lessons.md` — the empirical-priors doc, hand-curated, dated, cites alert IDs. Loaded into the analyst prompt at runtime. **This is the project's ground-truth style for evidence claims.**
- `journal/helenus.jsonl` — append-only ground truth (alert/outcome/review records).
- Claude auto-memory `confluence-rework.md` (outside repo) — the only written record of the rework decisions and the pending VALIDATION phase. **Single-point-of-failure risk**: not in the repo.
- `.env.example` — required secrets contract.

## 7. Known stale docs / contradictions (verified)
- README Setup says create `.venv` + `pip install`; reality: global Python, no venv, no pytest.
- README gate precedence and scalp description (1m/OTM) predate: GEX decoupling (`GexConfig.levels_in_signal`, default False), 5m/ITM scalp (`ScalpConfig.confluence_5m`, default True), Sq9, conviction/zones, LIQ_REJECT trigger. The working tree (uncommitted, ~1,560 insertions) is the truth.
- README lists commands without `!sq9 !conv !zones` (they exist in `bot.py`, verified).
- `lessons.md` documents that the journal field `vanna_active` logs False on every alert (dead field) and that the 53–66 confidence band does not discriminate winners from losers.

## 8. Known failure modes (from lessons.md, code comments, memory)
- Confirmation triggers fire into absorption at held levels (the 7340–7350 short cluster).
- ES order-book imbalance was almost promoted to a veto; graded reconciliation demoted it to a partial tell (see `lessons.md` 2026-06-25 header).
- Raw SPX volume is garbage (the observed 10.98× SPCFD artifact) — SPY proxy is mandatory.
- VIX1D end-of-day distortion mechanically ramps the index into the close (vanna trigger suppressed late-day).
- Mid-range entries, thin-room magnets, stacked same-side walls — the graded loser profiles.
- Restart drops in-flight excursion tracking (alerts journaled, grades lost).
- Git history is uninformative ("Add changes" ×4) — code + journal + memory are the archaeology, not commit messages.

## 9. Current highest-risk live problem
The **confluence-rework VALIDATION phase** (owner-confirmation-needed as "hardest"):
all four build tasks are done but uncommitted; provisional Sq9 modifier weights are ON
without validation; zone `min_score=35` is Pine's uncalibrated default; per-confluence
expectancy has not been computed; back-tagging of historical alerts hasn't happened; the
journal has only ~29 graded alerts (small-n). A weaker session could plausibly "finish"
by finalizing weights without evidence — the exact failure the skills must prevent.

## 10. Skills the repo clearly needs
change-control; repo-orientation; build-test-env; debugging-playbook;
failure-archaeology; architecture-contract; research-discipline (evidence bar);
signal-lifecycle (idea→flag→fixture→replay→grade→lesson→weight); the
confluence-validation campaign; market-domain-reference (0DTE/GEX/Schwab);
diagnostics-tooling; validation-and-qa; docs-and-writing; research-frontier;
analyst-prompting (the `_SEC_*` assembly + cache discipline is load-bearing and unique).

## 11. Skills that would be premature
- Anything MT5/Forex/EA (wrong project — template artifact).
- Backtesting-framework or holdout-management skills (no backtester exists; the "data"
  is a 29-alert live journal + one tape fixture).
- CI/CD or deployment skills (no CI, single-machine run).
- Auto-execution/risk-sizing skills (the bot alerts; it does not trade).

## 12. Questions the repo cannot answer (→ owner)
1. Is the confluence VALIDATION phase the hardest current problem, or is something else burning?
2. What unwritten discipline rules exist beyond "one task at a time, plan-first-wait-approval, flag-gated, hand fixtures"?
3. What do cheaper models / past sessions most often get wrong here?
4. Which past failure cost the most time (to weight the archaeology skill)?
5. What is the bar for "go live" / enabling the default-off flags (gexmap, cumdelta, approach, inflection)?
