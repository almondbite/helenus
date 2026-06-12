# Helenus

SPX/SPY 0DTE structure bot. Pulls the `$SPXW` PM-settled chain from the Charles
Schwab Developer API, computes dealer gamma exposure locally with vectorized
Pandas, runs the **scan2** mechanical trigger engine, and posts color-coded
Discord embeds. *Helenus reads. You aim. Not financial advice.*

## Layout

```
helenus/
  config.py             secrets + engine tuning (dataclasses)
  data/schwab_feed.py   schwab-py AsyncClient, $SPXW 0DTE fetch, AdaptiveThrottle
  engine/gex.py         chain JSON -> DataFrame -> Net GEX, walls, zero-gamma
  engine/scan2.py       pure trigger state machine (Triggers 1/2/3, confidence)
  output/embeds.py      Discord embed builders (green/red discipline, footer)
  bot.py                discord.py event loop + worker topology
main.py                 entrypoint
scripts/authorize.py    one-time interactive OAuth (writes token file)
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env     # fill in tokens
python scripts\authorize.py   # one-time browser OAuth -> schwab_token.json
python main.py
```

## Design notes

- **`$SPXW`, not `$SPX`** — the weekly root plus `from_date == to_date == today`
  is what isolates the PM-settled 0DTE contract set.
- **GEX convention** — `Call GEX = OI * Γ * 100 * spot`,
  `Put GEX = -OI * Γ * 100 * spot`; net per strike; zero-gamma is the
  interpolated sign flip of cumulative net GEX.
- **Volume proxy** — SPX prints no volume, so scan2's 20-period volume baseline
  is built from SPY cumulative-volume deltas sampled by the macro loop.
- **Throttling** — `AdaptiveThrottle` enforces a hard inter-call gap and widens
  the chain poll interval in premarket and when 5-minute realized range is dead.
- **Engines are pure** — `engine/` has no I/O and no awaits, so triggers and
  GEX math are unit-testable with canned JSON fixtures.

## Known structural placeholders (wire before going live)

1. `bot.py` bar assembly uses the chain-poll spot for bar high/low. For real
   sweep detection (Trigger 2) you want intra-bar extremes — switch to the
   schwab-py `StreamClient` level-one equity stream and aggregate true OHLC.
2. VIX "range boundaries" cold-start with a ±1.5 fallback band until enough
   history accumulates; persist `vix_history` to disk if you want real
   multi-day bands across restarts.
3. Confidence weights in `scan2._confidence` are first-pass priors — tune
   against your own journal, not in production.
