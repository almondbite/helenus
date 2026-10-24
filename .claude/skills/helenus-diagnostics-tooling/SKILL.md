---
name: helenus-diagnostics-tooling
description: Measure instead of eyeballing — Helenus's replay/grading scripts (replay_today, replay_gate, grade_today, ingest_today), Discord boards (!scan !gex !stream !stats), and journal queries. Use when you need to know what the gate would have fired, how alerts graded, what a board shows, or where an artifact lands.
---

# Helenus diagnostics and tooling

## 1. Purpose
Index of the instruments that turn "I think" into "I measured", and what each one
can and cannot tell you.

## 2. When to use
Tuning analysis, retrospective "what did we miss today", grading questions, verifying
live state, locating output artifacts.

## 3. When not to use
Interpreting whether measured results constitute evidence → `helenus-research-discipline`.

## 4. Inputs required
Scripts need `PYTHONPATH=.` and (for Schwab pulls) valid credentials. Boards need the
live bot + Discord access (they are owner-side instruments; a coding session usually
can't run them — ask the owner for board output).

## 5. The instruments

### Offline scripts (`scripts/`, run as `PYTHONPATH=. python scripts/<x>.py`)
| Script | Does | Output | Insufficient for |
|---|---|---|---|
| `replay_today.py` | pulls TODAY's REAL 1m tape from Schwab (today-only as written — verified) | `journal/tape_<date>.json` (gitignored) | past dates without a script change; anything intraday-forward-looking |
| `replay_gate.py` | feeds a real tape through `detect_candidate` (as written it hardcodes `tape_2026-06-22.json` — point it at another tape by editing the script's load path) | stdout candidate list | profitability (no grading), volume triggers (index tape has no volume → VOLUME_CONFIRMATION out of scope), flow/GEX context unless synthesized in-script |
| `grade_today.py` | 5-min session view + grades journaled alerts vs real forward tape | stdout | alerts outside the tape's date |
| `ingest_today.py` | appends `outcome` records + session `review` / lessons update | `journal/helenus.jsonl`, `lessons.md` | MUTATING — run only when the owner wants the day ingested; append-only, once |
| `resend_alerts.py`, `send_test_alert.py`, `check_channel.py`, `check_history.py`, `diagnose_channel.py`, `test_commands_bot.py` | Discord ops/diagnostics | — | production signal logic |

### Live Discord boards (owner-side)
`!gex` gamma board · `!sq9` ring lattice · `!conv` conviction · `!zones` zone lifecycle ·
`!charm` · `!scalp` · `!disp` · `!orb` · `!moc` · `!stream` feed freshness board ·
`!scan` tape internals (incl. gexmap position_state/cell/prior when enabled) · `!flow`
volume+vanna · `!inter` intermarket · `!stats` deterministic scorecard (accuracy, avg
MFE/MAE, early-reversal rate, by trigger — no Claude call) · `!review` Claude pattern
review (also auto at 16:00 ET).

### Journal queries (the raw evidence)
```bash
# record counts
python -c "import json,collections;print(collections.Counter(json.loads(l)['type'] for l in open('journal/helenus.jsonl',encoding='utf-8')))"
# one alert + its outcome by id
python -c "import json;[print(json.dumps(r,indent=1)) for r in map(json.loads,open('journal/helenus.jsonl',encoding='utf-8')) if r.get('id')=='<ID>' or r.get('alert_id')=='<ID>']"
```
(Outcome→alert linkage field is `alert_id` — verified in `helenus/journal.py` 2026-07-05.)

### Where artifacts land
`journal/helenus.jsonl` (append-only), `journal/lessons.md`, `journal/tape_<date>.json`
(gitignored), `schwab_token.json` (secret, repo root). NOTE: the whole `journal/` dir
is gitignored — the graded ground truth exists ONLY on this machine; treat it as
irreplaceable. Session scratch analysis → the session scratchpad, never `journal/` and
never `.claude/skills/` (the skills dir is the doctrine store, not a workspace).

## 6. Evidence required
Quote the command and the relevant output lines; name the artifact path.

## 7. Output artifact
Measured numbers with their source command; for retrospectives, the standard flow is
`replay_today` → `grade_today` → (owner-approved) `ingest_today`.

## 8. Common traps
- **Bad behavior this prevents:** a session hand-scans candlesticks in a tape JSON and
  asserts "the gate missed 4 obvious setups" — instead of running `replay_gate.py` and
  reporting what `detect_candidate` actually returns.
- Running `ingest_today.py` casually — it permanently appends outcomes and updates lessons.
- Reading `!stats` per-trigger cells as significant (single-digit n).
- Expecting replay to exercise VOLUME_CONFIRMATION (documented out of scope in the script header).

## 9. Related skills
`helenus-research-discipline`, `helenus-debugging-playbook`,
`helenus-confluence-validation-campaign`.

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `ls scripts/` and `head -10 scripts/replay_gate.py`
- `grep -n "commands.command\|async def cmd_" helenus/bot.py | head -20`

Last generated: 2026-07-05
Known uncertainty:
- (Both prior unknowns settled 2026-07-05 by factual review: linkage field is `alert_id`; `replay_today.py` is today-only as written.)
