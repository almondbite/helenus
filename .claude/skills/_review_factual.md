# Factual review (Reviewer 1) — skill library vs. repository

Reviewed 2026-07-05 against the working tree at `C:\Projects\Helenus` (branch `main`,
HEAD `9f4d75c`, rework uncommitted). Every command below was actually run; every
file:line was actually read. Scope: all 15 SKILL.md files, reference.md/templates, and
the 5 meta files.

---

## Verified correct

**Environment / tests**
- Global Python 3.12.10; `python -m pytest --version` → "No module named pytest"; no `.venv`; `requirements-dev.txt` exists but uninstalled; no `.github`/CI/linter config.
- `tests/` contains exactly 22 `test_*.py` files. All 22 ran green standalone with `PYTHONPATH=. python tests/test_<x>.py`. Claimed counts exact: `test_gex.py` 10, `test_scan2.py` 26, `test_conviction.py` 27. (Full run: approach 12, bot 3, charm 13, conviction 27, cumdelta 12, displacement 10, embeds 3, es_lead 10, flow 14, gex 10, gex_decouple 19, gexmap 14, intermarket 15, journal 19, moc 9, orb 9, scalp 20, scalp5m 16, scan2 26, sq9 25, stream 11, zones 15.)
- `.env.example` exists with exactly the claimed secrets (Discord token/channel, Schwab keys, Anthropic key). `main.py`, all 11 `scripts/*.py`, `helenus/data/schwab_feed.py` + `schwab_stream.py`, `helenus/output/embeds.py`, `helenus/journal.py` all exist as named. `pine_files/` holds `Displacement_Oscillator` ("DISPLACEMENT OSCILLATOR v6.0") and `Displacements_Atlas` ("DISPLACEMENT ZONES [Volatility Engine] v6.0") — the conviction and zones port sources respectively.

**Config (`helenus/config.py`) — all claimed defaults exact**
- `GexConfig.levels_in_signal=False` (L96), `ScalpConfig.confluence_5m=True` (L223), `itm_target_delta=0.65` (L228), `ConvictionConfig.enabled=True` + `fire_line=60.0` (L579/585), `ZoneConfig.enabled=True` + `min_score=35.0` (L620/626), `Sq9Config.enabled=True` + `modifier_enabled=True` + `step_degrees=45.0` (L662/663/672), `FeedbackConfig.early_window_bars=4` / `early_reversal_pts=3.0` (L703/704), `reflect_each_alert=False` (L705), `review_hour=16` (L706).
- Default-off flags as claimed: `GexMapConfig.enabled=False` (L535), `CumDeltaConfig.enabled=False` (L440), `ApproachConfig.enabled=False` (L494), `FlowConfig.inflection_enabled=False` (L192). `StreamConfig.enabled=True` (L409) — "default on" claim correct.
- `CumDeltaConfig.veto_conf_cap` exists (L451, =40.0) — the market-domain-reference/reference.md citation is valid.
- MOC window 15:50–15:55 (L327–330), VIX1D suppressed via `vix1d_distort_hour/minute` (L183–184), premarket throttle 120s (L157), forward grading window 30 bars (L694).

**Code symbols**
- `scan2.TriggerType` (L62–79) includes `LIQ_REJECT = "Liquidity Sweep Reject"` (L76). `detect_regime_flip` (L338), `detect_candidate` (L362), `detect_approach` (L818) all exist. `gexmap.map_gating_active` (gexmap.py:68), `bot._post_regime_flip` (bot.py:834), `CANDIDATE_COOLDOWN_SECS` (bot.py:68) exist.
- **Gate precedence CONFIRMED** (the architecture-contract skill's flagged uncertainty): with both rework flags on, scan2.py L592–597 builds `(_cd_candidate, _displacement_candidate, _liq_reject_candidate, _scalp_candidate, _flow_candidate)`, preceded by the window-gated MOC reversal (L423) and capitulation (L438) checks, i.e. **MOC/CAP → CD → displacement → liq-reject → EMA(5m) → flow**, exactly as invariant 7 states. The Task-2-only and full-legacy fallback orders in the code comment (L584–591) also match the docstring in the skill.
- `analyst._assemble_system_prompt` (analyst.py:679) takes exactly 4 flags: `levels_in_signal, scalp_5m, sq9_enabled, dispx_enabled` — the 4th (unverified in the skill) defaults from `CONFIG.conviction.enabled` (L694–695). All cited `_SEC_*` names exist (`_SEC_SCALP_LEGACY`/`_SEC_SCALP_5M`, `_SEC_SQ9`, `_SEC_DISPX`, `_SEC_CLOSING`, etc.). `SYSTEM_PROMPT = _assemble_system_prompt(...)` at import (L743). `cache_control: ephemeral` on the methodology block (L1386) with lessons appended AFTER it (L1389–1390); reviews run `include_lessons=False` (L1864). The verdict JSON schema requires exactly `has_signal, direction, confidence, thesis, risk_flags` (L761–772). The `gex_map` block leads the snapshot and is gated on both `gexmap` and `levels_in_signal` (L1319–1321). Confidence clamp 95 (L1623).
- Legacy pins: `legacy_scalp()` in tests/test_scan2.py:46 ✔; explicit `LEGACY = dataclasses.replace(CONFIG.scalp, confluence_5m=False)` in tests/test_scalp.py:49 ✔; `gex_mode()` ctx swapping `CONFIG.gex` via `object.__setattr__` + `dataclasses.replace` in tests/test_gex_decouple.py:48–59 ✔ (but see Finding 1 on `legacy_gex`).
- Sq9/zones contracts: `sq9.assess` returns a bounded delta (sq9.py:163; applied bot.py:919, analyst.py:1616–1625); neither `sq9` nor zone edges appear anywhere in scan2's `key_levels` (zones appear only in `detect_approach` arm context, scan2.py:915–919). Journal back-tag fields verified live in bot.py: `sq9_anchor/…/sq9_dist_to_ring_at_entry/sq9_reacted/sq9_coincide/sq9_modifier_delta/sq9_next_target_dist` (L924–937), `conv_signed` (L950), `zone_relation` (L965), `pre_armed` (L906).
- Engine facts: gexmap `position_state` values `{PINNED_AT_WALL, IN_OPEN_SPACE, APPROACHING_WALL, APPROACHING_FLIP, OVERSHOT_ENVELOPE}` (gexmap.py:400–427); `degraded` disclosure in conviction.py (11 hits) and zones.py (9); "VOLUME REWIRING" + the 10.98× SPCFD artifact documented in conviction.py:23; zones spawn weights 18/18/12/12/10/12/10/8 (zones.py:44–45, sum 100); Sq9 formula `level(θ) = (√A + 2·θ/360)²` (sq9.py:21,64) and 45° ≈ 43pt at SPX 7400 checks out arithmetically (7443.06); charm computed analytically (BS ∂Δ/∂t, charm.py:35–36,106–114) with LOW/BUILDING/HIGH intensity (L147–156); cumdelta quote rule with tick-rule fallback and carry-prior-sign (cumdelta.py:24–25,49–58); `_filter_contract_root` and `AdaptiveThrottle` (schwab_feed.py:41,89); `_backfill_state` (bot.py:699).

**GEX formula (skill claim vs `helenus/engine/gex.py`)**
- Confirmed in both docstring (L11–12) and code: `sign = np.where(side=="call", 1.0, -1.0)`; `gex = sign * openInterest * gamma * contract_multiplier(=100) * spot` (L97–104). Zero-gamma = interpolated first sign-flip of CUMULATIVE net GEX (L185–202). The symmetric-prior and regime language matches `GexProfile.regime` and the gexmap docstring.

**Discord commands (`bot.py` L1212–1396)**
- Exactly the 15 claimed: `!gex !sq9 !conv !zones !charm !scalp !disp !orb !moc !stream !scan !flow !inter !stats !review`. README's list (L468–472) indeed omits `!sq9 !conv !zones` — the capability-map drift claim is correct.

**Journal / lessons**
- `journal/helenus.jsonl` parses to exactly `Counter({'alert': 29, 'outcome': 29, 'review': 6})` — the 29/29/6 claim is exact.
- **Outcome→alert linkage field settled** (skills flagged it unverified): it IS `alert_id` (journal.py:249, and `graded_alerts` joins on it at L300). Alert records carry `id` = `uuid.uuid4().hex[:8]` (journal.py:180–181), so the skills' `r.get('id')=='<ID>'` query pattern works with the exact 8-hex IDs.
- `lessons.md` header exact: `_Updated 2026-06-25 20:00 UTC from 26 graded alerts (34.6% accurate). Hand-curated 2026-06-25: …demotion, not veto…_` — date, count, accuracy, and the ES-imbalance reconciliation wording all match the skills. Sections are What works / What misses / Tuning notes as claimed; the docs-and-writing correction template mirrors the real header format.
- Every cited alert ID exists in the jsonl (78ab2967, cc861b57, b0bd0bf8, 65744777, c0fd93fa, 6752da44, c19bdb9c, 69b53bf7, 1f93e950, 95fafc39, 47374a6c, 1a250eb4, 14ac62a9, 14436eee, 06febcff, 624d3944, 155957a5, 9d0d2470, 962aaef4, 9fb6cf50, 4627a704). Spot-checked numbers match the graded outcomes: 78ab2967 MFE 21.78 / context `es_imbalance: 0.63` ✔; 95fafc39 29.77; 1f93e950 27.39; 69b53bf7 26.41; 6752da44 41.27; cc861b57 MAE 33.4; b0bd0bf8 MAE 24.84; c0fd93fa MFE 0.3. The "53–66 confidence band" is exact: min 53.0, max 66.0 across all 29 alerts.

**Scripts / replay**
- `replay_gate.py` header confirms the skill claims verbatim: index candles print no volume, so it exercises SWEEP_RECOVER + LEVEL_REJECTION; "VOLUME_CONFIRMATION needs the SPY proxy and is out of scope here". Ran it offline: 18 candidates over 390 real bars in both profiles — works with no credentials, as the skills say. It synthesizes GEX in-script (matches "flow/GEX context unless synthesized in-script").
- `grade_today.py` ("Grade today's journaled alerts against the real tape… 5-minute view") and `ingest_today.py` (appends `outcome` records + a `review` + lessons update; idempotent) headers match the diagnostics-tooling table, including ingest's mutating nature.
- **replay_today.py date question settled** (skills flagged unverified): as written it can pull TODAY ONLY — `today = now_et().date()`, start hardcoded to 9:30 today (L25–27). The underlying `get_price_history_every_minute` call accepts arbitrary start/end datetimes, so past dates need only a trivial script variant (Schwab's minute-history retention is the remaining unknown). The campaign skill's "pull more days via replay_today.py equivalents if Schwab history allows" is accurately hedged.

**README / git / misc claims**
- README Setup does describe `.venv` (L493–494) — stale vs reality. "tune…against this, not in production" (L485, 579), "Known structural placeholders (wire before going live)" (L566), VIX ±1.5 cold-start band (L573), `vix_history` persistence placeholder (L574), scalp premium still on the 15s poll path "a candidate follow-up" (L335), MOC textbook 7375P "0.54 → 9.90 into 4:00" (L282–283) — all present as cited.
- git log: exactly 8 commits, 4 titled "Add changes" — "×4" and "8 commits, half titled" both exact. `git diff --stat`: 11 files, 1,560 insertions — matches "~1,560 insertions".
- `journal/tape_2026-06-22.json` is gitignored as claimed (`.gitignore:58 journal/`).
- Each of the 15 `evals/trigger_eval.json` files has 21 cases with `query/should_trigger/expected_skill` — satisfies the "20+ queries" claim and the trigger-matrix format note.
- Single `journal.log_alert` call site (bot.py:1084) in the signal path — consistent with "briefings/WATCH embeds are never graded".

---

## Findings

### BLOCKING
None found. No skill claim would send an engineer down a dangerously wrong path; the load-bearing claims (gate precedence, config defaults, GEX signs, test commands, journal counts) are all exact.

### IMPORTANT

1. **`legacy_gex()` is misattributed to `tests/test_gex_decouple.py`.**
   - Files/claims: `helenus-change-control/SKILL.md` L31–33 ("see `tests/test_gex_decouple.py` `legacy_gex()`…"); `helenus-validation-and-qa/SKILL.md` L27 ("`test_gex_decouple.py` (`legacy_gex()`/`gex_mode()` ctx…)"); `_repo_capability_map.md` L68–69.
   - Evidence: `test_gex_decouple.py` defines `gex_mode()` (L49) and `gexmap_enabled()` (L63) — no `legacy_gex`. `legacy_gex()` is defined in `tests/test_scan2.py:33`, `tests/test_approach.py:47`, and `tests/test_es_lead.py:28`.
   - Correction: cite `legacy_gex()` in test_scan2.py/test_approach.py/test_es_lead.py, and `gex_mode()`/`gexmap_enabled()` in test_gex_decouple.py. The mechanism description (contextmanager, `object.__setattr__` + `dataclasses.replace`) is accurate everywhere it appears.

2. **`_bar_volume` is attributed to scan2; it lives on the bot.**
   - Files/claims: `helenus-failure-archaeology/SKILL.md` L47 ("scan2's `_bar_volume`"); `helenus-failure-archaeology/reference.md` L59 ("`scan2._bar_volume`").
   - Evidence: `_bar_volume` is a method of the bot — `helenus/bot.py:681` (used at L379; SPY bar-boundary read documented at L557). `grep _bar_volume helenus/engine/scan2.py` → no match.
   - Correction: cite `bot._bar_volume` (helenus/bot.py). The substance (SPY cumulative-volume deltas at bar boundaries) is correct.

### MINOR

3. **"`vanna_active` logs False on every alert" is overbroad vs the current jsonl.** Claimed in `helenus-failure-archaeology/SKILL.md` item 4 + reference.md §4, `_expert_distillation_notes.md` red-flag table. Evidence: 28/29 alerts log False, but alert `06febcff` (2026-06-25 10:34, Capitulation Candle) logs `vanna_active=True`. The doctrine core survives — all three Vanna Momentum alerts (95fafc39, 1f93e950, 65744777) log False, so the field carries no discriminating signal — and lessons.md itself uses the "every alert" phrasing, but a session scripting `all(not va)` against the jsonl will be surprised. Suggest: "False on 28/29 alerts including every Vanna Momentum winner (single True: 06febcff)".

4. **VIX1D distortion window start: skills say "~15:30", config says 15:45.** `helenus-debugging-playbook` symptom row ('VIX1D "spiking" after ~15:30 ET'), `helenus-market-domain-reference/SKILL.md` session clock ("~15:30+ VIX1D distortion"), `_expert_distillation_notes.md` ("after ~15:30 ET"). Evidence: `FlowConfig.vix1d_distort_hour=15, vix1d_distort_minute=45` (config.py L183–184). Suggest citing 15:45 (or "suppressed from 15:45 ET per `vix1d_distort_*`").

5. **"22 config classes" is off by one.** `_repo_capability_map.md` L20. Evidence: config.py has 21 `@dataclass` definitions (20 tuning classes + `HelenusConfig`). Suggest "21 config dataclasses (20 tuning + HelenusConfig)".

6. **`replay_gate.py` is hardcoded to one tape, not "over `journal/tape_*.json`".** `helenus-research-discipline` tier-2 row, `helenus-signal-lifecycle` stage 4, `helenus-diagnostics-tooling` table. Evidence: the script opens `journal/tape_2026-06-22.json` and stamps `"2026-06-22"` into timestamps (replay_gate.py L38–39, 62). Other tapes require editing the script (or a bespoke copy). Suggest noting "hardcoded to tape_2026-06-22.json as written; generalize per-tape".

7. **Task-4 replay counts are not reproducible from any repo artifact, and "from 30 raw bars" garbles the source.** `helenus-signal-lifecycle` L45 ("6 LIQ_REJECT candidates from 30 raw bars, 40 zone spawns"); `helenus-confluence-validation-campaign` V3 ("40 spawns, 8 live at close"). Evidence: replay_gate.py passes `conviction=None` into `detect_candidate` (L44) so it can never emit LIQ_REJECT, and computes no zones; the tape has 390 bars, not 30. The auto-memory record says "6 LIQ_REJECT candidates (30 raw **rej** bars filtered by fire-line), 40 zone spawns…8 live zones at close" — i.e. 30 raw liq-reject bars filtered to 6, from a bespoke rework-session replay. Suggest rewording to "6 LIQ_REJECT candidates (30 raw sweep-reject bars filtered by the fire line)" and labeling the counts as from the rework session's bespoke replay (not reproducible via replay_gate.py as shipped).

8. **`Signal` name collision in analyst-prompting.** `helenus-analyst-prompting/SKILL.md` L45–46 ("structured outputs → `Signal` with `has_signal, direction, confidence, thesis, risk_flags`") and the trap "Renaming `Signal` fields". Evidence: those five fields are exactly `_SIGNAL_SCHEMA` (analyst.py:761–772) — a JSON schema/dict verdict, not a class. The class actually named `Signal` (scan2.py:100–111) has different fields (`trigger, direction, level, spot, volume_ratio, confidence, trend_label, notes, scalp`). The field list is right; the name is wrong. Suggest "`_SIGNAL_SCHEMA` verdict (dict)" and, for the trap, "renaming verdict-schema or scan2.Signal fields…".

9. **The quoted AST syntax-gate command is not the allowlisted one.** `helenus-build-test-env` L47–49 says the two-file command (`['helenus/config.py','helenus/bot.py']`) is "permission-allowlisted". Evidence: `.claude/settings.local.json` allowlists two exact-string variants with 4- and 5-file lists (config/journal/analyst/[embeds/]bot); the 2-file variant shown would still prompt. Cosmetic — the command itself works — but the "allowlisted" claim only holds for the exact stored strings.

10. **Default-off flag inventories omit `ESLeadConfig.enabled=False`.** `helenus-change-control` table, `_expert_distillation_notes.md` flag list, `_distillation_plan.md` failure-mode list all enumerate gexmap/cumdelta/approach/inflection. Evidence: `ESLeadConfig.enabled=False` (config.py L472) is a fifth owner-gated default-off flag. Nothing stated is false; the list is incomplete.

11. **Observation: the entire `journal/` directory is git-ignored** (`.gitignore:58`; `git ls-files journal` is empty). The skills correctly call tape files gitignored, but "journal/helenus.jsonl (append-only ground truth)" and lessons.md exist only on this machine — the same single-point-of-failure the capability map flags for auto-memory. Worth one sentence in repo-orientation or the capability map.

### Settled items the skills flagged as unverified (all resolved in the skills' favor or now answerable)
- Outcome-record linkage field = `alert_id` (journal.py:249) — diagnostics-tooling can drop its caveat; its dual `id`/`alert_id` query already works.
- Gate precedence order — CONFIRMED exactly as written (scan2.py:592–597); architecture-contract can drop its "confirm before citing" hedge.
- 4th `_assemble_system_prompt` flag = `dispx_enabled` (from `CONFIG.conviction.enabled`), analyst.py:683.
- GEX formula variable names — verified in code, not just README (gex.py:97–104); market-domain-reference can drop its "README-sourced" uncertainty.
- replay_today.py — today-only as written; arbitrary dates need a script variant (see Verified section).

---

## Overall verdict

The library is factually strong. Every command quoted runs as described on this machine (all 22 standalone suites green with the exact claimed pass counts, no pytest, global 3.12.10), every config default and flag state cited is exact against `helenus/config.py`, the journal claims (29/29/6, the 2026-06-25 / 26-alert / 34.6% lessons header, every cited alert ID and MFE/MAE figure, the literal 53–66 confidence band) reconcile against the real jsonl and lessons.md, and the highest-stakes flagged uncertainty — the gate precedence order — is confirmed verbatim in `scan2.detect_candidate`. No BLOCKING findings. The two IMPORTANT findings are wrong file attributions for real symbols (`legacy_gex()` placed in test_gex_decouple.py instead of test_scan2/test_approach/test_es_lead; `_bar_volume` placed in scan2 instead of bot.py) that would cost an engineer a few minutes of grepping, and the MINOR items are precision fixes (vanna_active is False on 28/29 not all 29 alerts, distortion window starts 15:45 not ~15:30, 21 not 22 config classes, replay_gate is single-tape-hardcoded, the Task-4 replay counts are memory-sourced and phrased misleadingly, a `Signal`-name conflation, and an incomplete default-off flag list). With those corrections applied, I found no remaining claim in the library that the repository contradicts.
