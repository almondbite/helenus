# Template: per-confluence validation report (Phase V4 deliverable)

```markdown
# Confluence validation report — <date>

## Decision requested
<The specific weight/default decisions the owner must make, one line each.>

## Data basis
Graded alerts: n=<N> (<date range>); tapes covered: <list>; back-tag coverage:
<which alerts could/could not be back-tagged per axis, and why>.
Trials declared: <every threshold/variant examined, including discarded ones>.

## Per-axis findings (one section per confluence, NEVER pooled)
### <Axis: e.g. Sq9 entry proximity>
- Cells: <state> n=<n> avgMFE=<x> avgMAE=<y> early-reversal=<z%>  [tier 3]
- Direction: <what the split suggests, in range language — no significance claims at n<15/cell>
- Veto test: would the proposed weighting have suppressed any graded winner? <IDs or "none">
- Degraded inputs: <per-alert degrades that limit this axis's back-tag>

## What this report does NOT show
<Mandatory. e.g. "persistence out of sample; any cell-level significance; ES-delta
components for pre-stream alerts.">

## Recommendations (evidence-tiered, no config changed)
- <axis>: <recommendation> [tier <k>, n=<n>] — fallback: <e.g. modifier_enabled=False>

## Uncertainties / owner questions
- <open items>
```
