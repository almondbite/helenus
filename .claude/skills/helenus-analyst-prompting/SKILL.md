---
name: helenus-analyst-prompting
description: The Helenus Claude-analyst layer — sectional _SEC_* system-prompt assembly keyed on 4 flags, prompt-cache discipline (cache_control on the stable methodology, lessons.md loaded after it), structured-output Signal schema, cost control. Use when changing analyst.py, the system prompt, snapshot blocks, lessons loading, or anything affecting Claude-call cost or the verdict schema.
---

# Helenus analyst prompting

## 1. Purpose
`engine/analyst.py` is the judgment layer and the main variable cost. Its prompt is
ASSEMBLED, CACHED, and FLAG-BRANCHED — naive edits silently multiply cost or desync
the prompt from the enabled feature set.

## 2. When to use
Any change to `analyst.py`, `_SEC_*` sections, snapshot `_*_block` builders, lessons
loading, the Signal schema, or Claude call parameters/cost.

## 3. When not to use
General Claude API questions → the `claude-api` skill. Non-analyst engines →
`helenus-architecture-contract`.

## 4. Inputs required
`helenus/engine/analyst.py` (read the assembly first), current flag states in `config.py`.

## 5. The mechanics (verified in analyst.py 2026-07-05)

- **Sectional assembly:** the system prompt is built by `_assemble_system_prompt(...)`
  from `_SEC_*` string constants, with LEGACY/DECOUPLED (GEX levels), SCALP_LEGACY/
  SCALP_5M, SQ9, and DISPX variants selected by four flags
  (`GexConfig.levels_in_signal`, `ScalpConfig.confluence_5m`, `Sq9Config.enabled`,
  conviction/zones). Module-level `SYSTEM_PROMPT = _assemble_system_prompt(...)` is
  built from CONFIG at import.
- **Rule: a flag's prompt section and its code path must move together.** Adding
  behavior behind a flag without a matching `_SEC_` branch means Claude judges with
  instructions for the wrong mode. This is the rework's established pattern — follow it.
- **Cache discipline:** the stable methodology prompt is `cache_control`-flagged so
  clustered gate calls reuse it. The `lessons.md` block loads AFTER the cached block —
  lesson updates must not invalidate the cache. Never insert dynamic content (dates,
  live numbers, lessons) into the cached section; never reorder so volatile content
  precedes it.
- **Snapshot, not prose:** Claude receives structured blocks (`_gex_block`,
  `_flow_block`, `_charm_block`, `_scalp_block`, `_conviction_block`, …) — spot, walls,
  distances, session phase. The `gex_map` block LEADS the snapshot when the map is
  enabled; per-alert dynamic context (e.g. an approach-arm) goes in the SNAPSHOT, not
  the cached prompt.
- **The verdict is typed:** structured outputs against `_SIGNAL_SCHEMA` in analyst.py
  (`has_signal, direction, confidence, thesis, risk_flags` — note this is the API
  response schema; `scan2.Signal` is a different type). `has_signal=false` (staying
  quiet) is a first-class outcome. `_SEC_CLOSING` demands JSON-only. Schema changes
  ripple to embeds, journal, and grading — treat as a cross-cutting change.
- **Claude judges, never computes:** hand it computed structure; never ask it to
  recompute GEX/targets. Conversely, don't move judgment (direction/conviction calls)
  into local heuristics — that's the gate's line.
- **Cost contract:** gate keeps calls to ~dozens/session. Anything adding calls
  (per-alert reflection is flag-gated `FeedbackConfig.reflect_each_alert` default off;
  reviews are 1/day + manual) or growing the uncached token load needs owner sign-off.
- **Reviews run WITHOUT lessons** loaded — so grading critiques raw outcomes rather
  than echoing its own priors. Preserve this when touching the review path.

## 6. Evidence required
For prompt changes: which `_SEC_` sections changed, which flag combinations were
re-assembled and syntax-checked (all 4 flags → run the assembly for representative
combos), and confirmation the cached block's content is byte-stable across calls.

## 7. Output artifact
A diff summary mapping: flag → section variant → snapshot block, plus a cache-impact
line ("cached block unchanged" or the cost justification).

## 8. Common traps
- **Bad behavior this prevents:** a session appends today's lessons text into the
  methodology section "for coherence" — every Claude call now re-pays the full prompt,
  silently multiplying session cost.
- Editing `SYSTEM_PROMPT` as if it were a literal (it's assembled; edit the sections).
- Adding a fifth flag to code but not to `_assemble_system_prompt`'s signature and all
  call sites (tests pin assembly variants — see `tests/test_gex_decouple.py`).
- Renaming `Signal` fields without touching journal/embeds/grading.

## 9. Related skills
`helenus-architecture-contract` (invariant 2/9), `helenus-docs-and-writing`
(lessons.md is live prompt input), `claude-api` (API-level reference).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `grep -n "_assemble_system_prompt\|cache_control" helenus/engine/analyst.py`
- `grep -n "_SEC_" helenus/engine/analyst.py | head -30`

Last generated: 2026-07-05
Known uncertainty:
- (4th flag VERIFIED 2026-07-05: `dispx_enabled`, driven by `CONFIG.conviction.enabled`. Still read the def before editing.)
