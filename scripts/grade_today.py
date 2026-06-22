"""
Grade today's journaled alerts against the real tape, and print a 5-minute
view of the session so missed-alert windows are visible.

    python scripts/grade_today.py
"""

from __future__ import annotations

import datetime as dt
import json

from helenus.config import CONFIG
from helenus.journal import grade_excursion


def load_tape(date_iso: str) -> list[dict]:
    with open(f"journal/tape_{date_iso}.json", encoding="utf-8") as f:
        return json.load(f)


def load_alerts(date_iso: str) -> list[dict]:
    out = []
    with open(CONFIG.feedback.journal_path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("type") == "alert" and r.get("ts", "").startswith(date_iso):
                out.append(r)
    return out


def main() -> None:
    date_iso = "2026-06-22"
    tape = load_tape(date_iso)
    by_t = {r["t"]: i for i, r in enumerate(tape)}

    # ---- 5-minute view of the day ----
    print("=== 5-min tape ===")
    for i in range(0, len(tape), 5):
        seg = tape[i : i + 5]
        o = seg[0]["o"]
        h = max(s["h"] for s in seg)
        l = min(s["l"] for s in seg)
        c = seg[-1]["c"]
        v = sum(s["v"] for s in seg)
        arrow = "+" if c > o else ("-" if c < o else "=")
        print(f"{seg[0]['t']}  O{o:8.2f} H{h:8.2f} L{l:8.2f} C{c:8.2f}  {arrow}{c-o:+6.2f}  v{v:>10,}")

    # ---- grade the journaled alerts ----
    print("\n=== alert grades (real forward tape) ===")
    window = CONFIG.feedback.forward_window_bars
    for a in load_alerts(date_iso):
        hhmm = dt.datetime.fromisoformat(a["ts"]).strftime("%H:%M")
        idx = by_t.get(hhmm)
        if idx is None:
            print(f"{hhmm} {a['trigger']} {a['direction']}: entry bar not in tape")
            continue
        fwd = tape[idx : idx + window + 1]
        entry = a["entry"]
        high = max(s["h"] for s in fwd)
        low = min(s["l"] for s in fwd)
        last = fwd[-1]["c"]
        oc = grade_excursion(a["direction"], entry, high, low, last, len(fwd) - 1)
        print(
            f"{hhmm} {a['trigger']:20s} {a['direction']:7s} @ {entry:.2f} conf {a['confidence']:.0f}"
            f"  -> {oc.grade:10s} MFE {oc.mfe_pts:5.1f} MAE {oc.mae_pts:5.1f}"
            f" ratio {oc.mfe_mae_ratio:4.2f} net {oc.net_pts:+.1f} over {oc.bars} bars"
        )


if __name__ == "__main__":
    main()
