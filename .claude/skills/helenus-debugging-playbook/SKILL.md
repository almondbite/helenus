---
name: helenus-debugging-playbook
description: Debug Helenus failures — no alerts firing, stream stale, Schwab 400/401 errors, wrong GEX numbers, missing volume, Discord embeds not posting, weird session/timezone behavior. Symptom-to-triage runbook with discriminating first checks. Use whenever live or replay behavior looks wrong.
---

# Helenus debugging playbook

## 1. Purpose
Route a symptom to its discriminating first check instead of wandering. Most "bugs"
here are data-source state, session windows, or documented quirks — not code.

## 2. When to use
Any "it's not working / looks wrong / stopped firing" report, live or offline.

## 3. When not to use
Test-suite failures with a clean assertion → `helenus-validation-and-qa`.
Environment/import errors → `helenus-build-test-env`.

## 4. Inputs required
The symptom, roughly when it occurred (ET), and if live: the `!stream`, `!scan`,
`!gex` board outputs.

## 5. Symptom table

| Symptom | First discriminating check | Likely root causes | False friend |
|---|---|---|---|
| No candidates all session | `!scan` — is the tape advancing? | market closed / premarket throttle widened; gate thresholds; `CANDIDATE_COOLDOWN_SECS` debounce | "the gate is broken" — it's designed to be quiet; dozens of Claude calls/session max |
| Stream data missing/frozen | `!stream` board — last-tick ages | staleness gate correctly falling back to poll; entitlement gap; ATM re-point lag after spot drift | blaming aggregation math (it's unit-tested — run `test_stream.py` first) |
| Chain fetch 400 | Was `$SPXW` used as underlying? | chains endpoint only accepts `$SPX`; SPXW is filtered client-side by `_filter_contract_root` | "API is down" |
| 401 / auth loop at startup | token file age | `schwab_token.json` expired → `python scripts/authorize.py` | rewriting feed code |
| GEX walls look wrong on 3rd Friday | contract roots in the chain | AM-settled monthly shares the date; the SPXW root filter must exclude it | "Schwab data is bad" |
| Volume zero / absurd | source of the bar volume | SPX prints no volume; SPY cumulative-delta proxy is the only source; raw feed showed a 10.98× SPCFD artifact | trusting a raw SPX volume field anywhere |
| VIX1D "spiking" after 15:45 ET | the clock | mechanical measurement-window distortion into the close; vanna trigger is deliberately suppressed (`FlowConfig.vix1d_distort_*`, window starts 15:45) | "vol event!" |
| Session levels wrong after restart | did `_backfill_state` run? | boot mid-session warms from 1-min history; if backfill failed, high/low/trend rebuild from empty | patching scan2 instead of the backfill |
| Alerts fired, grades missing | restart between alert and maturity | in-flight excursion tracking is memory-only (known placeholder) | "journal is corrupted" |
| Embeds not posting | `scripts/check_channel.py` / `diagnose_channel.py` | channel ID/permissions; Discord token | rewriting embeds.py |
| Times off by hours | tz of the timestamp | all session logic is ET; naive/UTC timestamps entering session windows | "scheduler bug" |
| Regime flip spam | flip embed is unscored info by design (rework) | `detect_regime_flip` posts muted embeds via `bot._post_regime_flip` | re-promoting it to a scored trigger |

## 6. Procedure
1. Reproduce or pin the timestamp (ET). 2. Run the discriminating check. 3. If a data
source is implicated, verify freshness/fallback BEFORE reading math. 4. If math is
implicated, run that engine's standalone suite (`PYTHONPATH=. python tests/test_<engine>.py`)
— green suite + wrong live value ⇒ input problem, not formula. 5. If offline
reproduction is possible, prefer the replay harness (`helenus-diagnostics-tooling`).
6. Stop and escalate to the owner when the fix would: change a flag default, alter gate
precedence, edit graded history, or touch live thresholds.

## 7. Evidence required
Root-cause claims need: the discriminating check's output, plus either a reproducing
command or the exact code path (file:line).

## 8. Output artifact
A triage note: symptom → check run → evidence → root cause (or ranked hypotheses) →
proposed fix WITH its change-control class.

## 9. Common traps
- **Bad behavior this prevents:** at 15:45 ET a session sees VIX1D ramping, concludes
  the vanna math is inverted, and "fixes" the sign — breaking a correct engine to match
  a documented distortion window.
- Diagnosing quiet as broken: the bot is engineered to be mostly silent.
- Restarting the bot as a "fix" — it drops in-flight grades (state loss is the cost).

## 10. Related skills
`helenus-diagnostics-tooling` (the instruments), `helenus-market-domain-reference`
(why the quirks exist), `helenus-failure-archaeology` (has this bitten before?).

## Provenance and maintenance

Re-verify volatile claims with:
- `grep -n "vix1d_distort" helenus/config.py`
- `grep -n "_filter_contract_root\|_backfill_state" helenus -r`

Last generated: 2026-07-05
Known uncertainty:
- Symptom table built from README design notes + code headers; live-incident frequency unknown (journal only records alerts, not ops incidents).
