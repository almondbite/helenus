---
name: helenus-confluence-validation-campaign
description: The executable campaign for Helenus's hardest current problem — validating the 2026-07 confluence rework: back-tag historical alerts, per-confluence expectancy, Sq9 role decision, zone min_score calibration, ITM delta confirm. Use when asked to "continue the rework", "validate the confluences", "finalize weights", or to EVALUATE/calibrate the new Sq9/zones/conviction/5m-scalp knobs (their math and mechanics belong to market-domain-reference; new-signal proposals to signal-lifecycle).
---

# Confluence validation campaign

## 1. Purpose
Owner-confirmed (2026-07-05) hardest problem: all four rework build tasks are DONE
(GEX decoupling, 5m/ITM scalp, Sq9, conviction/zones port) but **uncommitted and
unvalidated** — Sq9 modifier weights are provisionally ON, zone `min_score=35` is
Pine's uncalibrated default. The deliverable is an **owner-reviewed report**, not a
weight change.

## 2. When to use
Any request to continue/finish/validate the rework, tune the new knobs, or judge the
new confluences. Also when a session proposes touching `Sq9Config`, `ZoneConfig`,
`ConvictionConfig`, or `ScalpConfig.itm_target_delta` values.

## 3. When not to use
Building NEW signals → `helenus-signal-lifecycle`. General evidence questions →
`helenus-research-discipline`.

## 4. Inputs required
`journal/helenus.jsonl`, `journal/tape_*.json`, the four engines + their configs, and
the owner available for the final review (the campaign ENDS at a report).

## 5. Campaign phases

**Phase V0 — State check.** `git status` (rework committed yet?); confirm flags:
`Sq9Config.enabled=True, modifier_enabled=True (provisional)`, `ZoneConfig` spawn
`min_score=35.0`, `ScalpConfig.confluence_5m=True, itm_target_delta=0.65`,
`ConvictionConfig.fire_line=60.0`. Count graded alerts:
`python -c "import json,collections;print(collections.Counter(json.loads(l)['type'] for l in open('journal/helenus.jsonl',encoding='utf-8')))"`
Branch: if alert count is still ~29, EVERY per-confluence split will be single-digit —
the report must say so and lean on replay + forward-tagging, not significance claims.

**Phase V1 — Back-tag.** Historical alerts predate the new engines, so their journal
records lack `sq9_*` / `conv_*` / `zone_*` fields. Recompute per alert from its
timestamp + entry price against the session tape. Coverage constraint (verified):
`scripts/replay_today.py` pulls TODAY only as written; stored tapes are the coverage
(`journal/tape_2026-06-22.json` as of 2026-07-05). Extending the script to past dates
is a code change → plan + owner approval first; otherwise back-tag only the covered
alerts and SAY SO. Recipe per alert (offline, `PYTHONPATH=.`): load the tape JSON,
replay its bars through a fresh `MarketState` (`scan2.Bar` → `push_bar`, as
`scripts/replay_gate.py` does) up to the alert timestamp, then compute the axis fields
at that state — `sq9.sq9_levels(anchor=session open)` → `dist_to_ring_at_entry` /
reacted / coincide; the zone engine's relation/state at entry; conviction score where
inputs are reconstructible (ES delta is NOT for pre-stream dates: list it degraded).
Write results to a NEW analysis file in the session scratchpad (NOT `.claude/skills/`,
NOT `journal/`), never mutate `helenus.jsonl`.

**Phase V2 — Per-confluence expectancy.** For each axis SEPARATELY (never pooled):
split graded outcomes by axis state (e.g. Sq9 near-ring vs far; zone TESTING vs CLEAR;
conviction tier; 5m-scalp vs legacy-scalp counterfactual where computable) and report
avg MFE, avg MAE, early-reversal rate, n per cell. Directions and ranges only where
n < ~15 per cell (expected).

**Phase V3 — Decision inputs (per open question):**
- **Sq9 entry role:** if near-ring entries grade no better, recommendation is
  `modifier_enabled=False` (demote to manual read + exit grid — the pre-planned fallback).
- **Sq9 exit grid:** do next-ring targets sit at usable 0DTE distances? 45° ≈ 43pt at
  SPX 7400; evaluate 22.5°/11.25° via `step_degrees` (analysis-only, don't change the default).
- **Zone min_score:** distribution of spawn scores on replay (Task 4 baseline —
  memory-sourced, not reproducible from any repo artifact: 40 spawns, 8 live at close
  on 2026-06-22) vs which zones price respected; re-derive the baseline yourself.
- **ITM delta 0.65:** premium behavior of the selected leg on graded scalp alerts.
- **Veto test** (mandatory): would any proposed weighting have suppressed a graded winner?

**Phase V4 — The report.** One document, using the template at
`.claude/skills/helenus-docs-and-writing/templates/validation_report.md`: per-axis
findings + n + tier labels (`helenus-research-discipline` ladder), recommendations
with explicit uncertainty, and NO config changes. Owner reviews → owner decides →
only then change control executes weight changes.

### Fenced-off wrong paths
- Changing any weight/default "as part of validation".
- Threshold-shopping until the back-tag flatters an axis (declare every trial).
- Pooled expectancy across axes; significance language on single-digit cells.
- "Fixing" the rework code while validating it (one task at a time).
- Mutating `journal/helenus.jsonl` to add back-tagged fields.

## 6. Evidence required
Every number in the report: alert IDs or replay commands + n. Every recommendation:
its evidence tier.

## 7. Output artifact
The Phase V4 report (markdown), plus the back-tag analysis file. Success = owner can
make each weight decision from the report alone.

## 8. Common traps
- **Bad behavior this prevents:** "Validation complete — I set `min_score=52` and
  lowered the Sq9 modifier to +3 based on the data." (Weight changes without the
  owner-reviewed report; probably from single-digit cells.)
- Forgetting the 5m-scalp axis because Sq9/zones are shinier.
- Reporting conviction back-tags without listing degraded components per alert.

## 9. Related skills
`helenus-research-discipline` (the evidence bar), `helenus-diagnostics-tooling`
(replay/journal instruments), `helenus-change-control` (what happens after owner review).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `git status --short` (once committed, update Phase V0)
- `grep -n "min_score\|modifier_enabled\|itm_target_delta\|fire_line" helenus/config.py`

Last generated: 2026-07-05
Known uncertainty:
- `replay_today.py` is today-only as written (verified 2026-07-05); whether Schwab's history endpoint can serve past-date 1m tapes at all — unverified.
- Whether the owner wants the report in-repo or in chat — ask at Phase V4.
