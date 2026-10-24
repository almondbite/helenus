---
name: helenus-market-domain-reference
description: Applied 0DTE market-structure knowledge Helenus depends on — GEX sign conventions, dealer hedging, vanna/VIX1D, charm, cumulative delta quote rule, MOC auction, Schwab API quirks (no $SPXW underlying, no cash-index stream, SPX prints no volume). Use when reasoning about WHY an engine works, checking sign conventions, or interpreting market behavior. Owner-flagged: market-structure misunderstanding is the #1 weaker-model failure here.
---

# Helenus market domain reference

## 1. Purpose
The domain knowledge a mid-level engineer lacks and a weaker model gets WRONG (owner's
top-ranked failure mode). Signs and mechanisms first; deeper mechanics in `reference.md`.

## 2. When to use
Touching any engine's math; interpreting boards/alerts; reviewing a change for
market-mechanics correctness; explaining behavior ("why did it fade the call wall?").

## 3. When not to use
Code-structure questions → `helenus-architecture-contract`. Symptom triage →
`helenus-debugging-playbook`.

## 4. Inputs required
None; this is reference. Verify code-side constants against the cited files.

## 5. The conventions (get these signs wrong and everything inverts)

- **GEX:** `Call GEX = OI·Γ·100·spot`; `Put GEX = −OI·Γ·100·spot`; net per strike;
  zero-gamma = interpolated sign flip of CUMULATIVE net GEX (`engine/gex.py`).
- **Regimes:** positive net gamma → dealers hedge AGAINST moves (mean-revert, pin,
  "slow grind"); negative → dealers hedge WITH moves (amplification, trend). The GEX
  prior is **symmetric**: positive gamma fades BOTH walls by location — never a
  baked-in direction (`engine/gexmap.py` docstring).
- **Charm (∂Δ/∂t):** positive net charm = SUPPORTIVE (dealer buy-back under price —
  the afternoon melt-up); negative = OVERHEAD. Scales with wing OI and ∝1/T →
  intensity LOW→BUILDING→HIGH into the last ~2h (`engine/charm.py`).
- **Vanna rally:** VIX1D falling + OTM call flow outpacing puts → dealers short those
  calls buy the underlying → mechanical lift. Bearish mirror = put-flow pressure.
  **VIX1D, never 30-day VIX** — and VIX1D's late-day ramp is a measurement artifact,
  suppressed by `FlowConfig.vix1d_distort_*`.
- **Cumulative delta (/ES):** level-one has no aggressor tag → sign each executed-volume
  increment by quote rule (last ≥ ask → buy, ≤ bid → sell) with tick-rule fallback,
  carry prior sign on flat ticks (`engine/cumdelta.py`). Executed flow is the signal;
  resting book depth is NOT. Price at a new extreme without CD confirming = absorption.
- **Futures lead cash:** /ES streams tick-by-tick; $SPX cash is poll-folded (~15s,
  Schwab streams no cash index) → arm on /ES thrust, confirm on SPX (`engine/es_lead.py`).
  Only /ES displacement is used, never /ES-vs-SPX levels, so basis is irrelevant.
- **MOC:** the 4:00 ET closing auction magnetizes passive flow; the 3:50–3:55 reversal
  is read from options premium basing + side volume surge ONLY (no imbalance feed
  exists in the Schwab API and none is needed) (`engine/moc.py`).
- **SPX prints no volume:** SPY cumulative-volume deltas at bar boundaries are the
  proxy; raw feed showed a 10.98× SPCFD artifact.
- **0DTE chain selection:** query underlying `$SPX` (`$SPXW` → 400), `from==to==today`,
  keep only PM-settled `SPXW` roots (`_filter_contract_root`) so the AM-settled monthly
  never pollutes GEX on shared-date 3rd Fridays.
- **Option-premium mechanics the scalp/MOC engines encode:** falling IV bleeds long
  premium (vanna headwind — "calls drag, puts carry" in a vol crush); level targets are
  pre-translated to premium via first-order delta + a gamma convexity term; capitulation
  wicks resolve undercut-then-reclaim (enter the reclaim, never the wick).
- **Session clock:** everything is ET. Key windows: 09:30 open (+15m ORB lock),
  15:45+ VIX1D distortion (per `FlowConfig.vix1d_distort_*`), 15:50–15:55 MOC
  reversal, 16:00 close/auto-review.

## 6. Evidence required
When citing a convention in a change, point at the implementing line (e.g.
`engine/gex.py` GEX formula) — not this file alone. This file can go stale; code can't.

## 7. Output artifact
Correct signs/mechanisms in whatever you were producing; for disputes, a note citing
engine + line.

## 8. Common traps
- **Bad behavior this prevents:** a session "simplifies" the GEX prior to
  positive-gamma-= -bullish, converting a symmetric fade-both-walls model into a
  directional bias — the exact misread the gexmap docstring warns against.
- Treating VIX1D's EOD ramp as a vol event; using 30-day VIX for vanna.
- Reading book imbalance as executed flow (the CD engine exists because they differ).
- Assuming charm is bullish per se — it follows which WING holds the OI.

## 9. Related skills
`helenus-architecture-contract`, `helenus-debugging-playbook`,
`helenus-failure-archaeology` (graded evidence on these mechanisms).

## 10. Provenance and maintenance

Re-verify volatile claims with:
- `grep -n "OI \* gamma \* 100\|100 \* spot" helenus/engine/gex.py` (convention unchanged?)
- `grep -n "vix1d_distort" helenus/config.py`

Last generated: 2026-07-05
Known uncertainty:
- (GEX formula signs VERIFIED in `helenus/engine/gex.py` code by factual review 2026-07-05.)
