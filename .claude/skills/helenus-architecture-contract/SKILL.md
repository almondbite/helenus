---
name: helenus-architecture-contract
description: Helenus load-bearing architecture invariants — pure-math engines, flag gates, candidate-gate precedence, "the map never pulls the trigger", Sq9/zones never initiate or veto, Claude judges but never computes. Use before designing or reviewing ANY engine/bot/scan2/analyst change, or when a proposal might bypass a boundary.
---

# Helenus architecture contract

## 1. Purpose
State the invariants that may not be bypassed, so changes are designed inside them.
These are owner decisions encoded in code, not style preferences.

## 2. When to use
Designing/reviewing any change to `helenus/engine/*`, `bot.py`, gate precedence,
prompt assembly, or data flow. Evaluating "wouldn't it be simpler if…" proposals.

## 3. When not to use
Market-mechanics questions (why charm is supportive) → `helenus-market-domain-reference`.
Process questions → `helenus-change-control`.

## 4. Inputs required
The proposed change and the module docstring of each engine touched (contracts live in
file headers — read them first).

## 5. The invariants

1. **Purity boundary.** Engines are deterministic, no I/O, no awaits — testable with
   canned fixtures. `engine/analyst.py` is the ONLY network engine (the Claude call).
   `data/` owns all Schwab I/O. Never add a network call or file write to an engine.
2. **Claude judges, never computes.** All arithmetic (GEX, charm, targets) is local;
   Claude receives structured state and returns direction/confidence/thesis. Never ask
   the model to recompute math; never let local code make the judgment call.
3. **The gate decides WHEN to spend a Claude call.** `scan2.detect_candidate` is cheap
   and mechanical; Claude calls are ~dozens/session. Anything that inflates call volume
   needs owner approval (cost contract).
4. **The map owns WHERE/WHETHER; triggers own WHEN.** GEX map, zones, charm, Sq9 are
   spatial context that scores or vetoes *location*; entries fire only on interaction
   events (cross, displacement, flow inflection, CD divergence, approach-confirm).
   A bare level touch must never fire an entry.
5. **Sq9 and zones never initiate and never veto.** Sq9 `assess()` returns a bounded
   modifier delta only; Sq9 rings and zone edges are deliberately NOT in scan2's
   key-level grid. Adding them there violates two owner decisions at once.
6. **Flag gates preserve legacy exactly.** Flag off ⇒ behavior byte-identical to
   pre-change; consumers of a disabled/stale source no-op or fall back to the poll path.
   Legacy tests are pinned to enforce this (`legacy_gex()` in `test_scan2.py`/
   `test_approach.py`/`test_es_lead.py`; `gex_mode()`/`gexmap_enabled()` in
   `test_gex_decouple.py`; `legacy_scalp()` in `test_scan2.py`).
7. **Gate precedence is an owner-decided ranking.** With both rework flags on:
   MOC/capitulation (window-gated) → CD divergence → displacement → liq-reject →
   EMA ignition (5m) → flow; leading reads outrank lagging ones. Reordering is a
   governance change, not a refactor. (Enum: `scan2.TriggerType`.)
8. **Degrade loudly.** Missing inputs pin to documented neutral values and are listed in
   the reading's `degraded` field (see `conviction.py`, `zones.py`). Never silently fake.
9. **Prompt assembly is sectional and cache-aware.** `analyst.SYSTEM_PROMPT` is built by
   `_assemble_system_prompt(...)` from `_SEC_*` constants keyed on 4 flags; the stable
   methodology block is `cache_control`-cached and `lessons.md` loads AFTER it so lesson
   updates don't invalidate the cache. See `helenus-analyst-prompting`.
10. **Journal is append-only**, grading is deterministic (no model in the grade), and
    briefings/WATCH embeds are never graded.
11. **Timezone: ET** for all session logic (open, MOC 3:50–3:55 window, VIX1D
    late-day suppression, 16:00 auto-review).

### Known weak points (do not "discover" and fix without approval)
- $SPX index tape is poll-based (~15s) — Schwab does not stream the cash index.
- Restart loses in-flight excursion tracking; VIX bands cold-start ±1.5.
- Scalp premium series still on the poll path (documented candidate follow-up).

## 6. Evidence required
For "this doesn't break the contract": which invariant(s) the change touches and the
legacy-pinned suites that stay green.

## 7. Output artifact
Design/review note mapping the change onto invariants 1–11, flagging any it bends.

## 8. Common traps
- **Bad behavior this prevents:** a session adds Sq9 rings to the key-level grid "so
  rejections can fire there" — quietly converting a modifier into a trigger source and
  violating invariant 5.
- Making an engine async "for consistency" with bot.py (breaks purity/testability).
- Collapsing the flag branches once "everyone uses the new mode" (kills the legacy
  parity guarantee before the owner retires it).

## 9. Related skills
`helenus-change-control`, `helenus-analyst-prompting`, `helenus-market-domain-reference`,
`helenus-validation-and-qa`.

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `grep -n "class TriggerType" -A 18 helenus/engine/scan2.py` (precedence enum intact?)
- `grep -n "_assemble_system_prompt" helenus/engine/analyst.py`

Last generated: 2026-07-05
Known uncertainty:
- (Precedence order VERIFIED 2026-07-05 against `scan2.py:592-597`: MOC/CAP → CD → displacement → liq-reject → EMA(5m) → flow with both rework flags on. Line numbers will drift; the enum + detect_candidate body remain the source.)
