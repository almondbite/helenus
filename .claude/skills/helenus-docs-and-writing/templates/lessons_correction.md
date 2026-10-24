# Template: lessons.md dated correction (append, never rewrite)

Add to the file header line, and append a reconciliation note in the affected section:

```markdown
_Updated <YYYY-MM-DD HH:MM UTC> from <N> graded alerts (<X>% accurate). Hand-curated
<YYYY-MM-DD>: <one-line summary of what was reconciled and the verdict, e.g.
"ES-imbalance claims reconciled against the graded records (demotion, not veto)">._
```

In the relevant section (What works / What misses / Tuning notes):

```markdown
- <CORRECTED CLAIM>. Previously read as <old claim>; reconciled <date> against
  <alert IDs with MFE/MAE>: <the evidence that forced the change — including any
  graded WINNER the old rule would have blocked>. Treat as <penalty|demotion|veto|
  no-signal> going forward.
```

Rules: cite alert IDs inline; keep the superseded reading visible in the sentence;
state the operational rule (what the analyst should DO), not just the statistic;
remember this text is loaded into the live analyst prompt — write it as an
instruction-grade prior.
