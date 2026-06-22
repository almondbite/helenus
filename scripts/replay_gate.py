"""
Replay the candidate gate over today's REAL 1-minute tape (true OHLC) to see
what the improved triggers would have flagged. Index candles print no volume,
so this exercises SWEEP_RECOVER + LEVEL_REJECTION on real price structure
(VOLUME_CONFIRMATION needs the SPY proxy and is out of scope here).

    python scripts/replay_gate.py
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd

from helenus.data.schwab_feed import ET
from helenus.engine.gex import GexProfile
from helenus.engine.scan2 import Bar, MarketState, detect_candidate


def _profile(call_walls=None, put_walls=None, zero_gamma=None) -> GexProfile:
    return GexProfile(
        spot=0.0,
        total_net_gex=0.0,
        zero_gamma=zero_gamma,
        call_walls=call_walls or [],
        put_walls=put_walls or [],
        by_strike=pd.DataFrame(),
    )


def replay(rows, gex, label, cooldown_bars: int = 5) -> None:
    state = MarketState(prior_close=None)
    hits: list[tuple[str, str, str]] = []
    last_fired: dict[str, int] = {}  # trigger+level -> bar index, debounce
    for i, r in enumerate(rows):
        ts = dt.datetime.strptime(
            f"2026-06-22 {r['t']}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=ET)
        state.push_bar(
            Bar(ts=ts, open=r["o"], high=r["h"], low=r["l"], close=r["c"], volume=0.0)
        )
        cand = detect_candidate(state, gex=gex)
        if cand is None:
            continue
        lvl = f"{cand.level.price:.0f}" if cand.level else "na"
        key = f"{cand.trigger.value}:{lvl}"
        if i - last_fired.get(key, -10**9) < cooldown_bars:
            continue  # same trigger+level fired too recently
        last_fired[key] = i
        hits.append((r["t"], cand.trigger.value, cand.reason))
    print(
        f"\n[{label}] {len(hits)} candidate(s) over {len(rows)} real bars "
        f"(debounce {cooldown_bars} bars):"
    )
    for t, trig, reason in hits:
        print(f"  {t}  {trig:18s} {reason}")


def main() -> None:
    with open("journal/tape_2026-06-22.json", encoding="utf-8") as f:
        rows = json.load(f)

    # 1) Pure price structure (empty walls) — session extremes + round grid only.
    replay(rows, _profile(), "price-structure only")

    # 2) Representative static GEX from today's journaled snapshots: put-wall
    #    cluster at 7475/7450/7400, call walls up at 7520/7530, zero-Γ ~7410.
    #    Approximate (real walls drift intraday) but shows the wall-rejection lift.
    gex = _profile(
        call_walls=[(7520.0, 4e8), (7530.0, 3e8)],
        put_walls=[(7475.0, -4e8), (7450.0, -3e8), (7400.0, -2e8)],
        zero_gamma=7410.0,
    )
    replay(rows, gex, "with representative walls")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
