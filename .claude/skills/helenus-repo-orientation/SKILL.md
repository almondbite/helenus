---
name: helenus-repo-orientation
description: Orient a new session in the Helenus repo — where things are, what the bot does, which docs to trust, what is stale. Use at session start, when asked "what is this project", "how does Helenus work", "where is X", or before any task in an unfamiliar area. First skill to load with zero context.
---

# Helenus repo orientation

## 1. Purpose
Give a zero-context session (model or human) an accurate map of Helenus in ~2 minutes,
including the traps: which docs are stale, what is uncommitted truth, what is generated.

## 2. When to use
- Session start on any non-trivial task.
- "What does this project do / where does X live / how does a signal get made?"
- Before trusting ANY architecture claim from the README.

## 3. When not to use
- You already know the file you need → go edit/read it.
- Deep questions about one subsystem → `helenus-architecture-contract` (invariants) or
  `helenus-market-domain-reference` (market mechanics).
- Environment/test setup → `helenus-build-test-env`.

## 4. Inputs required
None. Read-only repo access.

## 5. Procedure
1. **What it is:** SPX/SPY 0DTE structure bot. Schwab API chain/quotes → deterministic
   local math (GEX, charm, flow/vanna, tape) → cheap mechanical candidate gate
   (`helenus/engine/scan2.py:detect_candidate`) → Claude analyst verdict
   (`helenus/engine/analyst.py`, the ONLY network engine) → Discord embed → graded
   append-only journal → lessons feedback into the analyst prompt. It alerts; it does
   not place trades.
2. **Map (verified 2026-07-05):**
   - `helenus/config.py` — every knob, frozen dataclasses, one global `CONFIG`.
   - `helenus/engine/` — pure-math engines, one file per edge: `gex` `gexmap` `charm`
     `flow` `scalp` `displacement` `conviction` `zones` `sq9` `orb` `moc` `cumdelta`
     `es_lead` `intermarket` `scan2` (gate) `analyst` (Claude).
   - `helenus/data/` — `schwab_feed.py` (REST poll + throttle), `schwab_stream.py`
     (flag-gated websocket; poll fallback on staleness).
   - `helenus/bot.py` — discord.py loop + `!` commands; `helenus/journal.py` — grading;
     `helenus/output/embeds.py` — rendering.
   - `scripts/` — OAuth + offline retrospective (`replay_today` `grade_today`
     `replay_gate` `ingest_today`).
   - `tests/` — 22 standalone hand-fixture suites (no pytest installed).
   - `journal/` — `helenus.jsonl` (append-only ground truth), `lessons.md` (empirical
     priors, loaded into the analyst prompt), `tape_*.json` (real-tape fixtures).
     NOTE: the entire `journal/` dir is gitignored — the graded evidence exists only
     on this machine. Treat it as irreplaceable; never bulk-edit or delete.
   - `pine_files/` — Pine sources the conviction/zones ports came from.
3. **Docs-of-record ranking (trust in this order):**
   1. The working tree + `git status` (the 2026-07 confluence rework may be uncommitted).
   2. `journal/helenus.jsonl` + `journal/lessons.md` (graded evidence).
   3. Module docstrings (each engine's contract lives in its header).
   4. `README.md` — good narrative, **stale-risk**: predates the confluence rework
      (GEX decoupling, 5m/ITM scalp, Sq9, conviction/zones, LIQ_REJECT) and its Setup
      section describes a `.venv` that does not exist.
4. **Check state before believing docs:** `git status` and `git diff --stat` — a large
   uncommitted diff means the tree, not the README, is current.

## 6. Evidence required
Any orientation claim you pass on must be traceable to a file you opened this session,
or be labeled with its source ("README says…, stale-risk").

## 7. Output artifact
A short orientation summary in the response (subsystem → file), or direct navigation to
the right file. No files written.

## 8. Common traps
- **Bad behavior this prevents:** a session reads README's gate-precedence paragraph and
  "fixes" scan2 to match it — reverting the uncommitted confluence rework. The README is
  the pre-rework bot.
- Assuming pytest/CI/venv exist (none do). See `helenus-build-test-env`.
- Treating `journal/*.jsonl` / `tape_*.json` as editable data — they're append-only /
  captured evidence.
- Believing git log ("Add changes" ×4) carries history — archaeology lives in
  `helenus-failure-archaeology`.

## 9. Related skills
`helenus-architecture-contract` (invariants), `helenus-build-test-env` (run/test),
`helenus-change-control` (before editing anything), `helenus-market-domain-reference`
(what GEX/charm/vanna mean).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `git status; git log --oneline -5`
- `ls helenus/engine helenus/data scripts tests journal`

Last generated: 2026-07-05
Known uncertainty:
- README may have been updated for the rework after this date — re-check before calling it stale.
