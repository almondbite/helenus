# Expert distillation notes (Phase 2)

Generated 2026-07-05. Owner-weighted risks (confirmed by owner this session):
**(a) market-structure misunderstanding, (b) trusting stale docs, (c) scope creep past
the one-task rule.** The evidence bar for the confluence rework: **back-tag + per-
confluence expectancy report reviewed by the owner BEFORE any weight finalizes.**

## 1. Expert heuristics

Rules an expert applies here that a weaker session will miss:

- **The working tree outranks the README.** The confluence rework (2026-07-04/05) is
  uncommitted; the README describes the pre-rework bot. When prose and `git diff`
  disagree, the diff is the truth. Check `git status` before believing any doc.
- **A flag's default IS the decision record.** `GexConfig.levels_in_signal=False`,
  `ScalpConfig.confluence_5m=True`, `Sq9Config.enabled=True (provisional)`,
  `GexMapConfig.enabled=False`, `CumDeltaConfig.enabled=False`,
  `ApproachConfig.enabled=False`, `ESLeadConfig.enabled=False`,
  `FlowConfig.inflection_enabled=False` — each default encodes an owner decision.
  Changing a default is a governance act, not a code tweak.
- **Sign conventions before arithmetic.** Call GEX = +OI·Γ·100·spot, Put GEX = −OI·Γ·100·spot.
  Positive net charm = SUPPORTIVE (bullish drift), negative = OVERHEAD. Cumulative delta
  signs by quote rule (last ≥ ask → buy) with tick-rule fallback. Positive gamma fades
  BOTH walls symmetrically — any model that turns the GEX prior into a fixed directional
  bias has misread the architecture.
- **The map never pulls the trigger.** GEX/zones/Sq9/charm own WHERE and WHETHER;
  interaction triggers (cross, displacement, flow inflection, CD divergence) own WHEN.
  Any proposal that makes a level touch fire an entry violates the core contract.
- **Sq9 and zones never initiate and never veto.** Sq9's `assess()` returns a bounded
  delta only; zone edges and Sq9 rings are deliberately NOT in scan2's key-level grid.
- **If a data source is degraded, degrade loudly.** Conviction and zones pin missing
  components to documented neutral values and list them in `.degraded` — never silently
  fake an input. Any new engine must follow this pattern.
- **Grade change ≠ regression when earliness is the goal.** Making signals earlier
  lowers per-signal hit rate by design; success is MFE-from-entry up AND early-reversal
  rate down. Don't "fix" a hit-rate dip caused by an earliness feature.
- **29 graded alerts is small-n.** Per-trigger splits have single-digit counts. An
  expert states directional findings ("mid-range reclaims cluster in the losers") and
  refuses point estimates ("EMA Ignition has 62% accuracy" from n=5 is noise).
- **Demotion beats veto until evidence forces it.** The ES-imbalance saga: a plausible
  hard veto would have blocked the +21.8 winner (78ab2967) and missed the two worst
  losers anyway (cc861b57, b0bd0bf8 had near-neutral ES). Reconciling claims against
  graded records turned a veto into a priced demotion. Default new filters to demotions.
- **Poll fallback must be exact.** Every stream consumer no-ops or falls back when the
  stream is stale; "when the flag is off, behavior is exactly as before" is a testable
  claim and the legacy tests are pinned to enforce it.
- **Tune against the journal/replay, never in production** (README's own rule).
- **Timezone is ET everywhere session logic runs.** Session phases, MOC windows
  (3:50–3:55), VIX1D distortion windows, the 16:00 auto-review — all ET. A naive local
  or UTC timestamp in session logic is a bug until proven otherwise.

## 2. Red-flag patterns

Phrases that must trigger verification before agreement:

| Phrase | Must check |
|---|---|
| "tests pass, so the signal works" | Fixtures prove math, not edge. Was it replayed on a real tape? Graded live? |
| "validated" | By what? Back-tag + per-confluence expectancy report reviewed by owner is the bar. A green suite or one replay is NOT validation. |
| "the README says" | README is stale-risk for the gate stack, scalp mode, commands, and Setup. Verify against the working tree. |
| "just enable the flag" | Default-off flags are owner decisions. Which flag, what's the fallback, who approved? |
| "small change" / "while I'm here" | One task at a time, plan first, wait for approval. Bundling is the named failure mode. |
| "high win rate" | n? Gross MFE or after early-reversal accounting? Confidence band 53–66 famously didn't discriminate. |
| "vanna_active shows" | Effectively dead field — False on 28/29 alerts incl. all vanna winners (lessons.md 2026-06-25 rounds to "every"). No usable signal. |
| "veto it" | The ES-imbalance lesson: prove a veto wouldn't have blocked graded winners before hardening a demotion. |
| "SPX volume" | SPX prints no volume; raw values show the 10.98× SPCFD artifact. Only the SPY proxy is valid. |
| "VIX1D is spiking into the close" | Mechanical measurement-window distortion after ~15:30 ET, not real vol. |
| "fixed" / "done" | Show the command output, the artifact path, or the alert IDs. Otherwise it's a claim. |
| "backtest" | No backtester exists. There is replay-of-gate on real tape and live journal grading. Don't invent one implicitly. |

## 3. Decision trees

**When a result looks good (a signal/threshold "works"):**
1. Is it hand-fixture green only? → math correct, edge unknown.
2. Was it replayed on a real tape (`journal/tape_*.json` + `scripts/replay_gate.py`)? → frequency/noise known; profitability still unknown (replay has no grading).
3. Is it back-tagged against graded journal outcomes? → now evidence, but check n per bucket.
4. Does the finding survive being conditioned per confluence separately (not pooled)?
5. Would the proposed gate have blocked any graded winner? (Run the ES-imbalance check.)
6. Is the claim gross MFE or net of the early-reversal metric?
7. Has the owner seen the expectancy report? → only then may a weight/default change.

**When Python behavior differs between flag-on and flag-off (parity):**
1. Is the flag-off path byte-identical to legacy? Legacy-pinned tests (`legacy_gex()`,
   `legacy_scalp()`, explicit LEGACY cfg) must stay green untouched.
2. If a legacy test needs editing to pass, the change is NOT additive — stop, re-plan.

**When a stream-fed value looks wrong:**
1. Is the stream fresh? (`!stream` board / staleness gate.) Stale → consumers must be
   on the poll fallback; check that first, not the math.
2. Is it an entitlement/subscription gap (option sub re-pointing at ATM as spot drifts)?
3. Only then suspect the aggregation math (which is unit-tested — run the suite).

**When asked to "finish" the confluence rework:**
1. The build tasks are done; do NOT re-do them or "improve" them unprompted.
2. The remaining work is VALIDATION: back-tag → per-confluence expectancy → report →
   owner decision. Producing the report IS the deliverable; changing weights is not.

## 4. Historical lessons (symptom → doctrine)

- **The 7340–7350 absorption cluster.** Symptom: repeated afternoon shorts graded
  INACCURATE at a held session-low shelf. Wrong path: tighten the trigger. Root cause:
  confirmation triggers lag by construction; buyers absorbed each break (visible in
  executed flow, not price). Doctrine: cumulative-delta divergence as veto/arm
  (`engine/cumdelta.py`); leading reads rank above lagging ones in the gate.
- **The ES-imbalance demotion.** Symptom: opposing /ES book imbalance correlated with
  losers. Wrong path: hard veto. Evidence: winner 78ab2967 fired against ES +0.63; worst
  losers had neutral ES. Doctrine: moderate priced penalty, overridable by the
  fresh-extreme with-trend profile. Encoded in `journal/lessons.md` (2026-06-25).
- **The confidence-band failure.** 53–66 confidence carried winners and losers
  indiscriminately. Doctrine: structural filters discriminate, numeric confidence
  doesn't; recalibrate the scale rather than trusting it.
- **The flow-magnitude trap.** The most extreme one-sided vanna flow on record
  (65744777, 29,548 vs 0) LOST; room + above-VWAP location carried the winners.
  Doctrine: condition vanna conviction on room and location, not flow magnitude.
- **The SPCFD volume artifact.** Raw SPX "volume" printed a 10.98× artifact. Doctrine:
  SPY cumulative-volume deltas at bar boundaries are the only volume input
  (`bot._bar_volume` in `helenus/bot.py`; conviction.py "VOLUME REWIRING").
- **Stacked-wall letter-vs-spirit.** c0fd93fa met the 8pt room rule at 8.6pt and still
  failed into three stacked put walls. Doctrine: stacked same-side walls absorb; the
  cluster keeps the full floor or stand down.
- **Git messages carry nothing.** 8 commits, half titled "Add changes". Archaeology
  lives in the journal, lessons.md, code docstrings, and Claude auto-memory — not git log.

## 5. Missing expert checks (not yet automated — encode as skill discipline)

- No script back-tags historical alerts with the new confluence fields (the validation
  campaign must build or hand-run this; `journal.py` + jsonl are the substrate).
- No per-confluence expectancy report generator (`!stats` splits by trigger, not by
  confluence axis).
- No automated legacy-parity run ("all legacy-pinned suites green with flags off") —
  it's a convention, run manually suite-by-suite.
- No check that README drift is flagged when engine contracts change.
- No restart-persistence for in-flight excursion tracking or `vix_history` (known
  placeholders; don't let a session "discover" and fix them without approval).
- `min_score=35` (zones) and Sq9 provisional weights carry no evidence yet — any output
  quoting them must label them uncalibrated/provisional.
