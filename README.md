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
  data/schwab_stream.py StreamClient websocket: option premium / /ES / SPY (live)
  engine/gex.py         chain JSON -> DataFrame -> Net GEX, walls, zero-gamma
  engine/charm.py       OTM-wing delta-decay (charm) -> support/overhead drift
  engine/scalp.py       1m 5/9 EMA-ignition scalp: gated cross + premium target
  engine/displacement.py institutional thrust: candle + FVG + MSS (+ sweep)
  engine/orb.py         opening-range breakout: locked range + volume/VWAP gates
  engine/moc.py         market-on-close: premium-behavior reversal + capitulation
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
   Claude call*. The power-hour **MOC plays** are checked first (window-gated), then
   the strict precedence is: a **regime flip** (spot crossing the zero-gamma pivot —
   the lead GEX edge), a **vanna / flow** trigger (a bullish vanna rally or its
   bearish mirror **put-flow pressure** — a *primary, standalone* signal that fires
   off the flow read regardless of any chart pattern), an **EMA ignition** (a gated
   1m 5/9 cross / 5-EMA reclaim — see below), a **displacement** (a high-volume
   institutional thrust candle — see below); then an **ORB breakout** (a confirmed
   close beyond the locked opening range), a **liquidity sweep**, a **level
   rejection** (probe-and-reverse at the *edge* of the session range — the range-day
   reversal), a **range-expansion** thrust between levels (the trend-day momentum
   signal), or a **level cross on volume**. The key-level grid
   includes the round-number grid, prior close, session high/low, **session
   VWAP**, **and the GEX walls + zero-gamma flip** so reactions fire at real
   structure, not just round numbers. Rejections only count near the running
   session extreme (`edge_proximity_pts`), so an interior wall price *pins*
   doesn't spam — and the bot further debounces candidates per
   trigger+level (`CANDIDATE_COOLDOWN_SECS`).
   The grid also carries the **charm support/resistance walls** (see below).
4b. `engine/charm.py` reads the same chain into a `CharmProfile` — the OTM-wing
   delta-decay structure (supportive vs overhead drift), keyed off live
   minutes-to-expiry so it ramps into the afternoon.
5. `engine/analyst.py` hands Claude the structured snapshot (options flow + vanna,
   GEX regime, walls, charm bias, tape, levels, VWAP, session phase / time-of-day,
   macro) and gets back a typed verdict via structured outputs: `has_signal`, `direction`,
   `confidence`, `thesis`, `risk_flags`. Claude may answer `has_signal=false` and
   stay quiet.
6. `output/embeds.py` renders the `Signal` to a color-coded Discord embed.

## The vanna rally (a primary trigger)

When 1-day implied vol falls, calls get cheaper — buyers pile into OTM calls.
Dealers short those calls must buy the underlying to stay hedged (vanna), which
mechanically lifts spot. `flow.py` watches for this: **VIX1D falling** while fresh
**OTM call flow outpaces OTM put flow** (the bearish mirror, **put-flow pressure**,
is VIX1D rising + OTM put flow → dealer selling). It's a **primary, standalone
trigger** — an extreme flow skew with VIX1D moving fires on its own, ranked just
under the GEX regime flip, regardless of any chart pattern. It's most potent as a
*reversal*: a hard-falling tape where VIX1D rolls over and call flow turns is a
high-conviction long.

**Driven by `$VIX1D`, not the 30-day `$VIX`** — VIX1D measures 0DTE implied vol, so
it reacts to the intraday vol crush the rally is built on. The catch is VIX1D's
end-of-day distortion: its measurement window mechanically shrinks into the close,
so it ramps near 4:00 ET for structural reasons, not real vol — the vanna trigger
is therefore **suppressed inside the late-day window** (`FlowConfig.vix1d_distort_*`).
`!flow` posts the options-volume summary (call/put volume split ITM/OTM, above vs
below spot) plus the live vanna read; a vanna-driven alert attaches it
automatically. Tunable in `FlowConfig` (`vix_drop_pts`, `min_call_flow`,
`call_dominance_ratio`).

## Charm — the afternoon melt-up (delta decay)

Charm is ∂Δ/∂t: how an option's delta bleeds away as time passes. For 0DTE it's
a dominant structural force with a specific dealer-flow story. Customers are net
long the OTM wings (lottery puts/calls); dealers are short them and hedge in the
underlying. As the wings decay, that hedge becomes too large and gets unwound —
mechanical flow with no conviction behind it:

- **OTM puts → positive charm → SUPPORTIVE.** Dealers were short futures to hedge
  the puts; as put delta decays they buy futures back, putting a floor under
  price. This is the engine of the low-volume **afternoon melt-up** — when the
  morning sell-off everyone hedged for never came, that hedge unwinds upward.
- **OTM calls → negative charm → OVERHEAD.** Dealers were long futures to hedge
  the calls; as call delta decays they sell, capping rallies and bleeding price
  lower into the close.

Two amplifiers: **open interest** (more OI in a wing = more delta to decay = more
futures to unwind — charm scales with OI) and **time** (charm ∝ 1/T, so it's
small in the morning and accelerates hard into the afternoon — `intensity` reads
LOW → BUILDING → HIGH, HIGH being the ≤2h melt-up window). `charm.py` computes it
analytically (Black-Scholes ∂Δ/∂t from each contract's own IV and live
minutes-to-expiry; Schwab doesn't emit charm the way it does gamma). `net_charm`
follows GEX's sign discipline: positive = supportive (bull drift), negative =
overhead (bear drift). The charm support/resistance walls join the key-level grid
(`Charm Support`/`Charm Resist`), and the bias feeds Claude as a *drift* input —
it raises conviction on with-charm setups (a bounce off charm support in a
supportive afternoon) and lowers it on against-charm ones. `!charm` posts the
structure board. Tunable in `CharmConfig`.

## EMA Ignition — the 1m 5/9 contract scalp (a primary momentum edge)

**One of Helenus's main edges.** A visual 0DTE scalp replicated mechanically: go
long when the option contract's 1-minute **5 EMA crosses above the 9 EMA** (or
reclaims the 5 EMA off a local bottom), targeting a structural level
**pre-translated into option premium**; the **200 EMA** is a dynamic
floor/ceiling. The literal edge is that charting the contract front-runs the index
and visualizes "gamma ignition." The weakness is that the raw 5/9 cross is
**noisy** — it whipsaws in chop, high-positive-GEX pins, and vanna (vol-crush)
tape. So the cross is the *trigger, not the edge — the edge is the filtering*, and
`engine/scalp.py` wraps it in a gated stack that is exactly what makes this a
primary setup rather than a noisy scalp:

- **Regime (Gate 0 — advisory, not a block).** Negative/transition GEX (green) is
  the friendly tape; high-positive GEX (red) **still fires** but is flagged
  `slow_grind` (dealer-damped) so the analyst tempers targets and conviction rather
  than the cross being auto-suppressed.
- **Vanna headwind (Gate 1).** Falling IV bleeds vega out of a long option's
  premium, so a strongly falling VIX1D is a premium headwind (the logged "calls
  drag, puts carry" asymmetry) — distinct from the vanna *rally* (a spot signal).
- **SPX confirmation (Gate 2).** SPX itself must confirm — price on the trade-side
  of session VWAP (falls back to the structural trend before VWAP is warm).
- **De-noising (Gate 3).** A **chop counter** (N+ 5/9 crosses in a tight window =
  chop), a **room-to-target** floor (**≥4pt** — lowered from 8 so smaller base hits
  aren't dropped; the analyst relaxes its magnet rule to ~4pt for the momentum edges
  to match), an acceptable **contract spread**, and a **dual-bleed** avoidance flag
  (both ATM wings bleeding = a GEX-pinned chop signature).

EMAs run on the clean continuous SPX **index** tape (`MarketState`); the selected
contract's **premium** is tracked separately for two boosters — the
**gamma-ignition front-run** (the contract's own 5/9 already crossed in-direction)
and **premium divergence** (the contract refuses to confirm the index's extreme,
vega being bid into a reversal). When a fresh cross clears the *entire* stack the
gate emits an `EMA Ignition` candidate carrying the **delta-targeted OTM strike**
and the **level→premium target** (first-order delta translation plus a gamma
convexity term); Claude still renders the verdict. `!scalp` posts the board.
Tunable in `ScalpConfig`. (Phase 2, deferred: the contract's own take-profit
spike, vega-extraction, theta-trendline, and volume-absorption *exit* signals,
which need per-contract volume/wick candles off a streaming feed.)

## Displacement — the institutional thrust

A displacement is the footprint of one-sided "smart money" flow: a sudden,
aggressive, full-bodied candle on heavy volume. The **hard gate is the candle
itself** — body ≥ an ATR multiple, mostly body (small wicks), on volume well above
the 20-bar baseline. The structural pillars are now conviction **boosters**, not
requirements (each has a `require_*` flag to harden it back into the gate):

- **Market structure shift (MSS)** — the thrust *closes past a recent swing*
  high/low, proof the trend actually turned rather than just wicking.
- **Fair value gap (FVG)** — a 3-candle imbalance (candle 1 and candle 3 wicks
  don't overlap), a high-quality **retrace-entry zone**, not a chase.
- **Liquidity sweep** — the move began by running stops beyond a prior swing then
  reversing (the trap).

It also reads the candle's **50% midpoint trend**: institutions defend the half-way
mark of their displacement, so price **holding ≥ 50%** of a bullish thrust = an
uptrend (look for **calls**), while a **close back below 50%** = they've flipped to
sellers (look for **puts**) — mirror for a bearish thrust. That `trend_direction`
is the freshest read of who's in control and leads Claude's directional call. The
reading is pure/stateless. `!disp` posts the board. Tunable in `DisplacementConfig`
(`require_fvg` / `require_mss` / `require_sweep`, `mid_fraction`).

## ORB — the opening range breakout

The first minutes after the 09:30 open carve out a range as liquidity floods in;
whichever way price *closes* out of it tends to set the session's direction.
`engine/orb.py` locks the high/low of the opening window (default 15 min), then
treats a 1-minute bar **closing** beyond the locked range as the breakout (long
above the high, short below the low — a close, not a wick, the standard fakeout
guard). Two **hard filters** gate the fakeout problem: **volume confirmation**
(the breakout bar clears a baseline volume multiple — institutional
participation) and **VWAP alignment** (a long is skipped while price is below the
day's VWAP, a short while above). Targets are **R-multiples** of the range height
(1R/2R) with the opposite edge as the stop. It's session-stateful (the locked
range outlives the bars that formed it), fires at most once per side per day, and
`warm`s from the backfill so a restart re-establishes the range without
re-alerting a past break. `!orb` posts the board. Tunable in `ORBConfig`.

## MOC — the power-hour close play

The 4:00 ET closing auction forces enormous passive flow (index funds, ETF
rebalances, fund creations/redemptions) to print at the close, so the tape gets
magnetized into it and tends to **reverse then drift** in recurring patterns.
Those patterns *drift* over time, so `engine/moc.py` surfaces them as priors and
**logs them** — every MOC alert's context (heuristic color, basing side, volume
surge, GEX state, capitulation stats) is journaled so the review → `lessons.md`
loop learns which still pay. No imbalance feed is used (it isn't in the Schwab
API and isn't needed); the play is read from mechanical proxies, deliberately
using **few variables**:

- **The MOC reversal (the priority play).** Between 3:50–3:55 price likes to
  reverse, read with a *simplified* version of the 0DTE premium-behavior strategy
  — **only options premium and volume**. When one side's (call/put) premium
  **bases** (prints a higher-low off a decline) while that side's fresh interval
  **volume surges** vs the opposing side, that side wins the reversal: a basing
  put side with surging put volume is a bearish reversal into the close (the
  textbook 7375P tell — the put based, put volume outran calls, and it ran 0.54 →
  9.90 into 4:00); a basing call side is bullish. Only fires inside the 3:50–3:55
  window.
- **The capitulation candle.** A contract's 1-minute **premium candle wicks far
  above its own 5/9/200 EMAs** (an "instant buy & sell book" mechanical order)
  then rejects with a red wick. It resolves **undercut-then-reclaim** — premium
  dumps well below the wick first, then reverses back past it — so the clean entry
  is the *reclaim after the undercut*, never the wick. This one fires **intraday
  too**, not just in the MOC window.
- **The 5-min candle-color heuristic.** The 5-minute index candle into 3:50,
  mapped *inversely*: green → dump into MOC (bearish), red → pump/uppercut
  (bullish). A weak drifting prior — it tilts a coin-flip, never carries a thesis.
- **GEX context.** "Buy into overshoot, sell into pin": whether spot is pinned at
  a wall / zero-Γ or has overshot the gamma envelope.

A **setup briefing** auto-posts a few minutes before 3:50 (mirroring the
pre-market briefing — a bias call, never graded), priming the window with the
heuristic, GEX state, and which side's premium is leading. The reversal /
capitulation candidates then fire live on top of it; Claude still renders every
verdict. Per-contract premium OHLC comes from the real-time **StreamClient** feed
(see below) when it's live — true 1-min wicks — and falls back to ~15s chain-poll
folding when it isn't. `!moc` posts the board. Tunable in `MocConfig`.

## Real-time streaming (schwab-py StreamClient)

Helenus is otherwise REST-poll driven, which approximates intra-bar extremes by
folding ~15s poll samples. `data/schwab_stream.py` adds a single **websocket**
connection as an **additive, flag-gated** layer (`StreamConfig.enabled`, default
on): when it's live the engines prefer streamed data, and when it's disabled or a
stream goes stale they fall back to the existing poll path — so nothing breaks if
streaming is unavailable or an entitlement is missing. It wires the three things
Schwab actually streams that move the needle:

- **`level_one_option`** on the ATM call/put → true per-contract premium ticks,
  folded into real 1-minute premium OHLC **with wicks**. The MOC capitulation
  candle literally can't see a 1-minute wick to 8.50 at 15s sampling; this is the
  feed it needs. The bot re-points the option subscription at the current ATM
  strikes as spot drifts, and `MocEngine.feed_stream` loads the streamed candle in
  place of poll-folded marks (same fields, so `on_bar` is source-agnostic).
- **`level_one_futures`** on `/ES` → real-time bid/ask size + volume, mapped into
  the inner-quote shape `engine/intermarket.py` already reads (so the ES
  microstructure refreshes on every tick instead of the 30s macro poll).
- **`chart_equity`** on SPY → authoritative 1-minute OHLCV, used for the tape's
  per-minute volume (the index *price* still comes from the `$SPX` poll — see the
  placeholder note; Schwab doesn't stream the cash index).

The connection runs a resilient login → subscribe → `handle_message` loop with
exponential reconnect backoff; handlers are sync and only update freshest-state,
which the bot pulls each bar/poll behind a staleness gate. `!stream` posts the
live status board (which streams are fresh, the ATM subs, last-tick ages). The
pure aggregation/mapping/diff logic is unit-tested without a socket; the live
socket is covered by a manual smoke test. Tunable in `StreamConfig`. (Scalp's
premium series stays on the 15s poll path for now — a candidate follow-up.)

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
- `!review` (and a daily auto-review at the market close, 16:00 ET) — Claude reads the graded
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

Discord commands: `!gex` (gamma board), `!charm` (charm / delta-decay board),
`!scalp` (EMA-ignition scalp board), `!disp` (displacement board), `!orb`
(opening-range board), `!moc` (market-on-close board), `!scan` (tape internals),
`!stream` (real-time feed status), `!flow` (volume summary + vanna), `!inter`
(intermarket board), `!stats`
(accuracy scorecard), `!review` (Claude pattern review).

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

The pure-math core — GEX (`engine/gex.py`), charm (`engine/charm.py`), the
EMA-ignition scalp (`engine/scalp.py`), displacement (`engine/displacement.py`),
the ORB engine (`engine/orb.py`), the MOC engine (`engine/moc.py`), the streaming
layer's pure pieces (`data/schwab_stream.py`), and options flow / vanna
(`engine/flow.py`) — is tested offline with hand-computed fixtures (no Schwab key,
no network):

```powershell
python tests\test_gex.py      # standalone runner, no pytest needed
python tests\test_charm.py
python tests\test_scalp.py
python tests\test_displacement.py
python tests\test_orb.py
python tests\test_moc.py
python tests\test_stream.py
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

1. `bot.py` bar assembly still aggregates the **$SPX index** intra-bar high/low
   from chain-poll spot samples (~15s) — because Schwab does **not** stream the
   `$SPX` index (indices are screener-only symbols), so the index price tape
   stays poll-based. The real-time `StreamClient` feed (below) covers what *is*
   streamable — option premium, /ES, and SPY — but not the cash index itself.
2. VIX "range boundaries" cold-start with a ±1.5 fallback band until enough
   history accumulates; persist `vix_history` to disk if you want real
   multi-day bands across restarts.
3. The candidate gate thresholds (`AnalystConfig.gate_volume_ratio`,
   `ScanConfig.sweep_pierce_pts`) tune how often Claude is consulted, and the
   `SYSTEM_PROMPT` in `analyst.py` is the methodology — tune both against your
   own journal, not in production.
