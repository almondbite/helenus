---
name: helenus-validation-and-qa
description: What counts as test evidence in Helenus — the 22 standalone hand-fixture suites, legacy-pinning context managers, how to add tests, how to read failures, and the hard limit on what a green suite proves. Use when writing tests, interpreting a test failure, claiming "tests pass", or reviewing test changes in a diff.
---

# Helenus validation and QA

## 1. Purpose
Define the test tiers, the house testing style, and the ceiling on test-based claims.

## 2. When to use
Adding/modifying tests; a suite fails; someone cites tests as proof; reviewing a diff
that touches `tests/`.

## 3. When not to use
Running mechanics/env issues → `helenus-build-test-env`. Whether a signal has EDGE →
`helenus-research-discipline` (a green suite is tier 1 of 5).

## 4. Inputs required
The suite file(s) and, for failures, the assertion output.

## 5. Test types
(These are TYPES of test, not the numbered evidence LADDER — tier numbering for
claims lives in `helenus-research-discipline` and is the only "tier" vocabulary.)

| Type | What | Where |
|---|---|---|
| Hand-fixture unit suites | pure engines vs hand-computable fixtures — "every Net GEX, wall, zero-gamma flip can be verified by hand" | `tests/test_*.py` (22 suites, verified counts: gex 10, scan2 26, conviction 27) |
| Legacy-parity pins | prove flag-off behavior is unchanged | `gex_mode()`/`gexmap_enabled()` ctx in `test_gex_decouple.py` (swapping `CONFIG.gex` via `object.__setattr__` + `dataclasses.replace`), `legacy_gex()` ctx in `test_scan2.py`/`test_approach.py`/`test_es_lead.py`, `legacy_scalp()` ctx in `test_scan2.py`, explicit LEGACY cfg in `test_scalp.py` |
| Replay on real tape | gate behavior on a real session | `scripts/replay_gate.py` + `journal/tape_*.json` |
| Live journal grading | the only performance type | `journal/helenus.jsonl` |
| Manual smoke | the live websocket (only untested-by-design piece) | run the bot; `!stream` |

Run: `PYTHONPATH=. python tests/test_<x>.py` → `ok` lines + `N passed`. No pytest, no CI.

## 6. Procedure

**Reading a failure:** the fixtures are hand-computed — a failing assertion means either
a real regression OR a deliberate behavior change that invalidates the hand computation.
NEVER "fix" the expected value to match new output without re-deriving it by hand; that
converts a spec into an echo.

**Adding tests (house style):** standalone runner (executable without pytest), no
network/keys, fixtures small enough to verify by hand, named `test_<engine>.py`,
new-behavior tests separate from legacy pins. Copy the shape of an existing suite.

**Legacy-pin rule:** if your change requires editing a `legacy_*`-pinned test, your
change is not additive — stop and go to `helenus-change-control`.

**Coverage claim rule:** "all suites green" means you looped ALL of `tests/test_*.py`
and report per-suite counts, not that three suites passed.

### What a passing suite CANNOT support
- "The signal works / has edge / is validated" (tier 1 of 5).
- "No live regression" (streams, throttles, Discord, session timing are outside the fixtures).
- "The Pine port is faithful" — fixtures verify the PYTHON spec; deliberate deviations
  from Pine are documented in `conviction.py`/`zones.py` docstrings (e.g. distinct-entry
  touch counting vs Pine's first-touch quirk) and the fixtures encode the DEVIATION.

## 7. Evidence required
Per-suite `N passed` lines. For fixture changes: the hand re-derivation in the PR/summary.

## 8. Output artifact
Test run transcript; for new tests, the suite file + its pass count + what the fixture
hand-computes.

## 9. Common traps
- **Bad behavior this prevents:** a suite fails after an engine tweak, and the session
  updates the expected wall price from 7450 to 7448.3 "to match" — silently rewriting
  the hand-computed spec instead of asking whether the tweak is wrong.
- Reporting pytest-style output that never ran (`pytest tests\` needs an install that
  doesn't exist).
- Testing through the bot loop instead of the pure engine (engines are pure BY DESIGN
  so tests don't need asyncio/Discord).

## 10. Related skills
`helenus-build-test-env` (how to run), `helenus-research-discipline` (tiers above tests),
`helenus-change-control` (legacy-pin governance).

## Provenance and maintenance

Re-verify volatile claims with:
- `ls tests/`
- `PYTHONPATH=. python tests/test_gex.py`

Last generated: 2026-07-05
Known uncertainty:
- Suite count (22) as of 2026-07-05; new engines add suites.
