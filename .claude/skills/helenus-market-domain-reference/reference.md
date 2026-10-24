# Deeper mechanics (load only when the SKILL.md summary isn't enough)

## Dealer-hedging causal chains (the WHY behind each engine)

**GEX / gamma.** Dealers are net short customer options. In positive net gamma their
hedge P&L forces buy-low/sell-high (stabilizing → pin, mean-revert, walls defended);
in negative net gamma they must sell into falls and buy into rallies (destabilizing →
amplification). Zero-gamma is where the sign of that feedback flips — hence
REGIME_FLIP as the lead spatial event and `position_state` in the gexmap
(`PINNED_AT_WALL, IN_OPEN_SPACE, APPROACHING_WALL, APPROACHING_FLIP,
OVERSHOT_ENVELOPE`).

**Vanna (∂Δ/∂vol).** IV falls → OTM call deltas rise toward spot exposure dealers are
short → they buy futures. Chain: vol crush → call buying → dealer hedge buying → spot
lift. That's why the trigger reads VIX1D (0DTE vol) + fresh OTM call-vs-put flow, and
why the *derivative* (flow acceleration + VIX1D slope rolling over) leads the level
thresholds (`FlowConfig.inflection_*`, default off).

**Charm (∂Δ/∂t).** Customers hold the lottery wings; dealers short them hedge in
futures. Wing delta decays → hedge oversized → unwind. Put-wing decay unwinds SHORT
hedges (buy-back → floor → melt-up); call-wing decay unwinds LONG hedges (sell →
ceiling). Amplifiers: wing OI (more delta to decay) and 1/T (accelerates into the
afternoon; `intensity` HIGH ≈ last 2h). Computed analytically (BS ∂Δ/∂t per contract
IV + live minutes-to-expiry) because Schwab doesn't emit charm.

**Cumulative delta.** Aggressor-signed executed /ES volume. Divergence at an extreme
(price new low, CD not) = the aggressor is being absorbed by passive resting interest
→ move spent BEFORE price turns. Veto shape: reactive trigger INTO absorption gets a
confidence cap (`veto_conf_cap`) or suppression in `hard` mode, keyed on Claude's
direction post-verdict; the CD_REVERSAL arm itself is exempt (it IS the read).

**MOC.** Passive index/ETF rebalance flow must print at 16:00 → the tape magnetizes
into it. Reversal read (15:50–15:55 only): one side's premium BASES (higher-low off a
decline) while that side's fresh interval volume surges vs the other side → that side
wins (textbook 7375P: based, put volume outran calls, 0.54 → 9.90 into 4:00).
Capitulation candle (fires intraday too): premium wicks far above its own 5/9/200 EMAs
then rejects; resolves undercut-then-reclaim — enter the reclaim.

## Premium/scalp mechanics
- Level→premium target: first-order delta translation + gamma convexity term (the
  contract gains delta as it moves ITM — targets are convex, not linear).
- Vanna headwind vs vanna rally: same greek, two uses — falling VIX1D LIFTS spot
  (rally, a spot signal) but BLEEDS long premium (headwind, a contract-P&L gate).
  "Calls drag, puts carry" in a vol crush.
- Gamma-ignition front-run: the CONTRACT's 1m 5/9 crossing before the index confirms —
  the reason the scalp charts premium at all. Premium divergence (contract refusing to
  confirm an index extreme) = vega being bid into a reversal.
- ITM leg under `confluence_5m` (calls ≤ spot / puts ≥ spot, target |Δ|≈0.65): higher
  delta ⇒ premium tracks the index more faithfully, less pure-vol exposure. 0.65 is
  provisional pending validation.

## Schwab API applied quirks (all encoded in `data/`)
- Chains: underlying must be `$SPX` ($SPXW → HTTP 400); isolate today via
  `from_date == to_date == today`; client-side root filter keeps PM-settled `SPXW`
  only (3rd-Friday AM monthly shares the date).
- Streams that EXIST: `level_one_option` (per-contract ticks; re-point subs at ATM as
  spot drifts), `level_one_futures` (/ES), `chart_equity` (SPY 1m OHLCV).
  Streams that DON'T: any cash index — $SPX price is poll-only forever (until Schwab
  changes; the root cause behind es_lead and the folding logic).
- `$SPX` quote's `closePrice` seeds the prior-session-close level; spot is sampled from
  BOTH chain polls and the 30s macro quote so intra-bar extremes don't starve on a
  dead tape.
- `AdaptiveThrottle`: hard inter-call gap; widens in premarket and when 5-min realized
  range is dead — "no data" premarket is usually the throttle working.

## Sq9 lattice (what it is and is not)
level(θ) = (√anchor + 2θ/360)², 45° steps, ±1 rotation, anchor default session_open.
Deterministic, reproducible from (anchor, step, rotations). At SPX ~7400 a 45° step
≈ 43pt (why `step_degrees` 22.5/11.25 is a validation question for 0DTE exits).
Gann angles/fans and time cycles are DELIBERATELY not implemented. Roles: bounded
confluence modifier on a tag-and-hold REACTION (never proximity alone) + exit/target
grid. Never initiates, never vetoes.
