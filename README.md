# Helenus

SPX/SPY 0DTE structure bot. Pulls the PM-settled SPXW 0DTE chain from the Charles
Schwab Developer API, computes dealer gamma exposure locally with vectorized
Pandas, and uses **Claude (Opus 4.8)** to read the structure and render the
trade verdict. A cheap mechanical gate decides *when* to spend a Claude call;
Claude makes the actual judgment. Color-coded Discord embeds out. *Helenus
reads. You aim. Not financial advice.*

## Layout

```
helenus/
  config.py             secrets + engine tuning (dataclasses)
  data/schwab_feed.py   schwab-py AsyncClient, SPXW 0DTE fetch, AdaptiveThrottle
  engine/gex.py         chain JSON -> DataFrame -> Net GEX, walls, zero-gamma
  engine/flow.py        0DTE call/put volume (ITM/OTM split) + vanna-rally tracker
  engine/scan2.py       pure rolling tape + key levels + candidate GATE (no verdict)
  engine/analyst.py     Claude reads the structured state -> Signal (the judgment)
  journal.py            MFE/MAE accuracy grading + append-only JSONL journal
  output/embeds.py      Discord embed builders (green/red discipline, footer)
  bot.py                discord.py event loop + worker topology
main.py                 entrypoint
scripts/authorize.py    one-time interactive OAuth (writes token file)
```

## How a signal is made

1. `data/schwab_feed.py` pulls the SPXW 0DTE chain + macro quotes (throttled).
2. `engine/gex.py` turns the chain into a `GexProfile` (Net GEX, walls, zero-Γ) —
   pure, deterministic math.
3. `engine/flow.py` builds the 0DTE options-volume distribution (call/put volume
   split ITM/OTM around spot) and runs the **vanna-rally tracker** (see below).
4. `engine/scan2.py` keeps the rolling bar tape + key-level grid and runs
   `detect_candidate` — a cheap gate that only decides whether a bar is *worth a
   Claude call*. It watches, highest-information first, for: a **flow setup**
   (bullish vanna rally, or its bearish mirror **put-flow pressure**), a
   **regime flip** (spot crossing the zero-gamma pivot), a **liquidity sweep**, a
   **level rejection** (probe-and-reverse at the *edge* of the session range —
   the range-day reversal), a **range-expansion** thrust between levels (the
   trend-day momentum signal), or a **level cross on volume**. The key-level grid
   includes the round-number grid, prior close, session high/low, **session
   VWAP**, **and the GEX walls + zero-gamma flip** so reactions fire at real
   structure, not just round numbers. Rejections only count near the running
   session extreme (`edge_proximity_pts`), so an interior wall price *pins*
   doesn't spam — and the bot further debounces candidates per
   trigger+level (`CANDIDATE_COOLDOWN_SECS`).
5. `engine/analyst.py` hands Claude the structured snapshot (options flow + vanna,
   GEX regime, walls, tape, levels, VWAP, session phase / time-of-day, macro) and
   gets back a typed verdict via structured outputs: `has_signal`, `direction`,
   `confidence`, `thesis`, `risk_flags`. Claude may answer `has_signal=false` and
   stay quiet.
6. `output/embeds.py` renders the `Signal` to a color-coded Discord embed.

## The vanna rally (the edge)

When VIX falls, implied vol drops, so calls get cheaper — buyers pile into OTM
calls. Dealers short those calls must buy the underlying to stay hedged (vanna),
which mechanically lifts spot. `flow.py` watches for this: **VIX falling** while
fresh **OTM call flow outpaces OTM put flow**. It's weighted as one of Claude's
most important inputs and is most potent as a *reversal* — a hard-falling tape
where VIX rolls over and call flow turns is a high-conviction long, not a reason
to stay bearish. `!flow` posts the options-volume summary (call/put volume split
ITM/OTM, above vs below spot) plus the live vanna read; a vanna-driven alert
attaches it automatically. Tunable in `FlowConfig` (`vix_drop_pts`,
`min_call_flow`, `call_dominance_ratio`).

## Accuracy feedback loop

Every intraday alert is graded **strictly on underlying SPX price action** over a
forward window (default 30 bars ≈ 30 min), using Maximum Favorable / Adverse
Excursion:

- **MFE** — largest move in the alert's favor; **MAE** — largest move against it
  (favorable is *up* for bullish calls, *down* for bearish).
- Deterministic grade (no model in the loop): **ACCURATE** = real favorable
  excursion with controlled risk (`MFE ≥ target` and `MFE/MAE ≥ ratio`),
  **INACCURATE** = large adverse move with no follow-through, else **MIXED**.

Alerts and outcomes are written to an append-only JSONL journal
(`journal/helenus.jsonl`, gitignored). On top of that:

- `!stats` — free deterministic scorecard (accuracy %, avg MFE/MAE/ratio, by
  trigger). No Claude call.
- `!review` (and a daily auto-review at 16:30 ET) — Claude reads the graded
  history and surfaces the **patterns** separating accurate from inaccurate
  alerts (regime, vanna state, trend alignment, confidence vs outcome), logged as
  a `review` record.
- `FeedbackConfig.reflect_each_alert` (default off) adds a per-alert Claude "why"
  note when each one matures — richer logs, one extra call apiece.

**The closed loop.** Each review distils its findings into a human-editable
lessons file (`journal/lessons.md`), which the analyst loads back into its system
prompt as empirical priors — so what graded out accurate/inaccurate steers every
*future* judgment. Lessons load at startup and refresh after each review. The
lessons block sits *after* the cached methodology prompt, so updating it doesn't
invalidate the prompt cache; the file is plain markdown, so you can hand-curate
it too. (Reviews themselves run without lessons, to grade raw outcomes rather
than echo their own priors.)

Pre-market briefings are excluded from grading (they're a bias call, not an
intraday entry). In-flight alerts are tracked in memory, so a restart drops their
excursion tracking (the alert is still journaled).

Discord commands: `!gex` (gamma board), `!scan` (tape internals), `!flow`
(volume summary + vanna), `!stats` (accuracy scorecard), `!review` (Claude
pattern review).

### Offline retrospective (`scripts/`)

For "what did the gate miss today?" analysis without a live session:

- `replay_today.py` — pull today's real 1-minute tape from Schwab to
  `journal/tape_<date>.json` (gitignored).
- `grade_today.py` — print a 5-minute view of the session and grade the day's
  journaled alerts against that real forward tape.
- `replay_gate.py` — feed the real OHLC tape through `detect_candidate` to see
  what the current triggers would have flagged (and how noisy they are); the
  harness to tune thresholds against, not in production.
- `ingest_today.py` — grade the day's alerts and append the `outcome` records +
  a session-retrospective `review`/`lessons.md`, closing the feedback loop from
  the real tape (the live grader would write the same outcomes).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env     # fill in tokens
python scripts\authorize.py   # one-time browser OAuth -> schwab_token.json
python main.py
```

## Tests

The pure-math core — GEX (`engine/gex.py`) and options flow / vanna
(`engine/flow.py`) — is tested offline with hand-computed fixtures (no Schwab
key, no network):

```powershell
python tests\test_gex.py      # standalone runner, no pytest needed
python tests\test_flow.py
python tests\test_journal.py

pip install -r requirements-dev.txt   # pulls in pytest
pytest tests\                          # discovers all suites
```

The fixtures are built so every Net GEX, wall, zero-gamma flip, and vanna trip
can be verified by hand against the assertions.

## Design notes

- **Querying 0DTE** — the chains endpoint only accepts the underlying `$SPX`
  (`$SPXW` is rejected with 400). `from_date == to_date == today` isolates
  today's expiration, then `_filter_contract_root` keeps only the PM-settled
  `SPXW` contracts so the AM-settled monthly (which shares the date on the 3rd
  Friday) never pollutes GEX.
- **GEX convention** — `Call GEX = OI * Γ * 100 * spot`,
  `Put GEX = -OI * Γ * 100 * spot`; net per strike; zero-gamma is the
  interpolated sign flip of cumulative net GEX.
- **Volume proxy** — SPX prints no volume, so scan2's 20-period volume baseline
  is built from SPY cumulative-volume deltas. The delta is read at each bar
  boundary (`_bar_volume`), so a bar's volume aligns to exactly that bar rather
  than to the async macro tick.
- **Spot sampling** — spot feeds the tape from both the chain poll and the 30s
  macro `$SPX` quote, so intra-bar high/low don't starve when the chain throttle
  widens on a dead tape. The `$SPX` quote's `closePrice` also seeds the
  prior-session-close key level.
- **Startup backfill** — on boot mid-session, `_backfill_state` warms `MarketState`
  from today's 1-min history ($SPX OHLC + SPY volume), so session high/low, the
  volume baseline, and trend are correct from the first live bar instead of
  rebuilding from an empty tape.
- **Throttling** — `AdaptiveThrottle` enforces a hard inter-call gap and widens
  the chain poll interval in premarket and when 5-minute realized range is dead.
- **GEX math stays deterministic** — `gex.py` and the `MarketState` tape have no
  I/O and no awaits, so they're unit-testable with canned JSON fixtures. Only
  `analyst.py` does network I/O (the Claude call).
- **Claude does the judgment, not the arithmetic** — the gamma math is computed
  locally and handed to Claude as structured input. The model decides *direction,
  confidence, and thesis*; it never recomputes GEX.
- **Cost control** — the gate keeps calls sparse (~dozens/session), and the
  stable methodology prompt is `cache_control`-flagged so clustered calls read it
  cheaply instead of re-paying for it.

## Known structural placeholders (wire before going live)

1. `bot.py` bar assembly aggregates intra-bar high/low from the running min/max
   of chain-poll spot samples between bar closes (~15s resolution) — enough to
   make sweep detection real. For tick-accurate extremes, switch to the
   schwab-py `StreamClient` level-one equity stream and aggregate true OHLC.
2. VIX "range boundaries" cold-start with a ±1.5 fallback band until enough
   history accumulates; persist `vix_history` to disk if you want real
   multi-day bands across restarts.
3. The candidate gate thresholds (`AnalystConfig.gate_volume_ratio`,
   `ScanConfig.sweep_pierce_pts`) tune how often Claude is consulted, and the
   `SYSTEM_PROMPT` in `analyst.py` is the methodology — tune both against your
   own journal, not in production.
