# Uncertainty register (Phase 7)

Honest inventory of what this library asserts WITHOUT full verification, as of
2026-07-05. Anything not listed here was verified by direct command/read in the
authoring session or the Phase 6 factual review (which ran all 22 test suites green
and checked every cited config default, alert ID, command, and symbol).

## Facts not verified (labeled in-skill where cited)
- Whether Schwab's price-history endpoint can serve PAST-date 1m tapes at all
  (`replay_today.py` is today-only as written — verified; the API's capability is not).
  Gates back-tag coverage for the validation campaign.
- The MOC "textbook 7375P" anecdote (0.54 → 9.90) — sourced from README prose; no
  journal artifact backs it.
- Live-incident frequencies behind the debugging symptom table (built from design
  notes and code headers, not an ops log — none exists).
- `AdaptiveThrottle` exact widening thresholds (described qualitatively only).
- The streaming layer's behavior under real entitlement gaps (manual smoke only,
  by design).

## Docs suspected stale (stale-risk)
- `README.md`: Setup section (venv that doesn't exist), gate stack / scalp mode /
  command list (pre-rework). Owner has not asked for the update; skills route around
  it rather than fix it.
- Claude auto-memory `confluence-rework.md` (outside the repo) is the ONLY record of
  the rework decisions — single-point-of-failure; this library now encodes its
  substance, but the owner should consider committing the rework + a decision record.

## Commands not run this session
- `python main.py`, `scripts/authorize.py`, `scripts/ingest_today.py`,
  `scripts/resend_alerts.py`, `scripts/send_test_alert.py`, Discord `!` boards
  (live/mutating/interactive — deliberately not exercised). Their descriptions come
  from source reading only.
- `scripts/replay_today.py` end-to-end against live Schwab (needs market-day creds).

## Repo areas not inspected in depth
- `helenus/data/schwab_stream.py` internals (claims come from README + docstrings +
  test_stream.py existence).
- `helenus/output/embeds.py` beyond its role; `scripts/check_history.py` /
  `diagnose_channel.py` beyond their names/headers.
- `pine_files/` sources were not diffed against the conviction/zones ports (the ports
  document their own deliberate deviations; fidelity was NOT independently re-derived).

## Historical claims needing owner confirmation
- Task-4 replay baseline (6 LIQ_REJECT from 30 raw rejection bars; 40 zone spawns,
  8 live at close) — memory-sourced, reproducible only by re-running the replay.
- That the four core discipline rules are exhaustive (owner said "those four are the
  core" 2026-07-05 — new rules may emerge during validation).
- Whether the research-discipline evidence bar generalizes beyond the confluence
  rework (assumed yes; flagged in that skill).
- The signal-lifecycle stage numbering is this library's formalization, not
  owner-ratified vocabulary.

## Skills needing real weaker-model evals
All 15. `_model_transfer_eval.md` defines the benchmark but has NOT been executed
against GPT 5.5 / Opus 4.8 / Sonnet-class sessions. Trigger evals are likewise
designed, not yet run through a real router. Until then, trigger quality and transfer
value are **candidate**, not known.

## Standing risks the library cannot remove
- `journal/` is gitignored: the graded ground truth exists only on this machine.
- Small-n (29 graded alerts) bounds every empirical claim in lessons.md and
  failure-archaeology; doctrine may legitimately change as n grows.
- The rework is uncommitted; if it lands differently than the working tree read on
  2026-07-05, skills citing its specifics need the maintenance pass.
