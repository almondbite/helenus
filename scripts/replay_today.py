"""
One-off: pull today's real 1-minute tape from Schwab so a missed-alert
retrospective runs on actual price action, not reconstruction.

Dumps candles to journal/tape_<date>.json and prints a compact summary
(open, high, low, close, session range, and the biggest 5/15-min swings).

    python scripts/replay_today.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

from helenus.data.schwab_feed import ET, SchwabFeed, now_et


async def main() -> None:
    feed = SchwabFeed()
    await feed.connect()
    client = feed._client

    today = now_et().date()
    start = dt.datetime.combine(today, dt.time(9, 30), tzinfo=ET)
    end = now_et()

    candles: list[dict] = []
    for sym in ("$SPX", "SPY"):
        try:
            await feed.throttle.gate()
            resp = await client.get_price_history_every_minute(
                sym,
                start_datetime=start,
                end_datetime=end,
                need_extended_hours_data=False,
            )
            resp.raise_for_status()
            data = resp.json()
            candles = data.get("candles", []) or []
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: fetch failed: {exc!r}")
            candles = []
        if candles:
            print(f"  {sym}: {len(candles)} candles")
            break

    await feed.close()
    if not candles:
        print("No candles returned. Cannot build the tape.")
        return

    def t(ms: int) -> str:
        return dt.datetime.fromtimestamp(ms / 1000, tz=ET).strftime("%H:%M")

    rows = [
        {
            "t": t(c["datetime"]),
            "o": round(c["open"], 2),
            "h": round(c["high"], 2),
            "l": round(c["low"], 2),
            "c": round(c["close"], 2),
            "v": int(c.get("volume", 0)),
        }
        for c in candles
    ]

    out_path = f"journal/tape_{today.isoformat()}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)

    hi = max(r["h"] for r in rows)
    lo = min(r["l"] for r in rows)
    print(f"\nSaved {len(rows)} bars -> {out_path}")
    print(f"Session: open {rows[0]['o']}  high {hi}  low {lo}  close {rows[-1]['c']}")
    print(f"Range: {hi - lo:.2f} pts")

    # Biggest forward swings over 5- and 15-bar windows, to point the eye at the
    # moves a silent gate may have missed.
    for win in (5, 15):
        best_up = best_dn = 0.0
        up_at = dn_at = ""
        for i in range(len(rows) - win):
            seg = rows[i : i + win + 1]
            base = seg[0]["c"]
            up = max(s["h"] for s in seg) - base
            dn = base - min(s["l"] for s in seg)
            if up > best_up:
                best_up, up_at = up, seg[0]["t"]
            if dn > best_dn:
                best_dn, dn_at = dn, seg[0]["t"]
        print(
            f"{win}-bar: max up +{best_up:.2f} from {up_at}  |  "
            f"max down -{best_dn:.2f} from {dn_at}"
        )


if __name__ == "__main__":
    asyncio.run(main())
