"""
Close today's feedback loop with REAL data:

  1. Grade each of today's journaled alerts against the actual 1-minute tape
     (pulled by replay_today.py) using the engine's own MFE/MAE grader, and
     append the resulting `outcome` records to the journal — exactly what the
     live OutcomeTracker would have written had the bot kept running.
  2. Persist a session-retrospective `review` + distilled lessons.md, so the
     analyst loads today's findings back into its prompt for future judgments.

The review text is the analyst's read of today's REAL tape (390 bars) plus the
two graded outcomes — richer than the stock review_patterns path, which only
sees alert context, never the tape. Idempotent: alerts already graded are skipped.

    python scripts/ingest_today.py
"""

from __future__ import annotations

import datetime as dt
import json

from helenus.config import CONFIG
from helenus.journal import Journal, LessonStore, grade_excursion

DATE = "2026-06-22"


def main() -> None:
    journal = Journal()
    records = journal.read()
    already = {r["alert_id"] for r in records if r.get("type") == "outcome"}
    alerts = [
        r
        for r in records
        if r.get("type") == "alert" and r.get("ts", "").startswith(DATE)
    ]

    with open(f"journal/tape_{DATE}.json", encoding="utf-8") as f:
        tape = json.load(f)
    by_t = {r["t"]: i for i, r in enumerate(tape)}
    window = CONFIG.feedback.forward_window_bars

    print("=== grading today's alerts on the real tape ===")
    for a in alerts:
        if a["id"] in already:
            print(f"  {a['id']} already graded — skipping")
            continue
        hhmm = dt.datetime.fromisoformat(a["ts"]).strftime("%H:%M")
        idx = by_t.get(hhmm)
        if idx is None:
            print(f"  {a['id']} entry bar {hhmm} not in tape — skipping")
            continue
        fwd = tape[idx : idx + window + 1]
        oc = grade_excursion(
            a["direction"],
            a["entry"],
            max(s["h"] for s in fwd),
            min(s["l"] for s in fwd),
            fwd[-1]["c"],
            len(fwd) - 1,
        )
        note = None
        if len(fwd) - 1 < window:
            note = (
                f"Forward window truncated at the close ({len(fwd) - 1}/{window} "
                "bars) — grade is on the available tape."
            )
        journal.log_outcome(a["id"], oc, note)
        print(
            f"  {a['id']} {hhmm} {a['direction']:7s} -> {oc.grade} "
            f"(MFE {oc.mfe_pts}, MAE {oc.mae_pts}, ratio {oc.mfe_mae_ratio})"
        )

    # ---- session retrospective -> review + lessons ----
    review = {
        "summary": (
            "2026-06-22 was a ~70pt range day (SPX 7460–7530). Helenus fired two "
            "alerts and graded 1 ACCURATE / 1 MIXED. The one directional call it "
            "made — the 10:31 negative-gamma breakdown — was excellent; the gaps "
            "were range-edge reversals it had no trigger for."
        ),
        "accurate_patterns": [
            "Negative-gamma + WITH-TREND break of a level at a fresh session low "
            "followed through hard (10:31 BEARISH @7487.57 graded ACCURATE: MFE "
            "26.5 / MAE 3.5, ratio 7.5). In an amplification regime, with-trend "
            "level breaks on participation are high quality — keep firing these.",
        ],
        "inaccurate_patterns": [
            "Missed the range edges entirely: the 7530 session-high rejection that "
            "kicked off the whole -70 slide, and the 7460 floor bounces (+8.5 at "
            "10:55, +16 into the close). Cause was structural — bars carried no "
            "intra-bar high/low (sweeps were blind to wicks) and GEX walls / "
            "session extremes weren't crossable levels.",
            "The 15:51 BULLISH call graded MIXED only because the 30-bar forward "
            "window ran into the close (8 bars, net +2.6) — a grading-window "
            "artifact, not a poor read.",
        ],
        "suggestions": [
            "Intra-bar high/low now aggregated from spot samples, and GEX walls + "
            "zero-Γ + session extremes are now crossable/rejection levels — sweep "
            "and the new LEVEL_REJECTION trigger should catch range-edge reversals.",
            "LEVEL_REJECTION discipline: the gate now only surfaces rejections at "
            "the EDGE of the session range (within ~10pts of the running high/low) "
            "via ScanConfig.edge_proximity_pts; interior-wall pins (the 7475 put "
            "wall mid-range today) are filtered out mechanically. A rejection you "
            "see is a real edge test — still confirm the wick is decisive and the "
            "edge held; a single-bar tap immediately retraced in POSITIVE_GAMMA is "
            "weak.",
            "Final-hour (after ~15:45 ET) intraday alerts: weigh that the forward "
            "grading window truncates at the close.",
        ],
    }
    # Idempotent: don't stack a second retrospective review on re-run.
    have_review = any(
        r.get("type") == "review" and r.get("summary", "").startswith(DATE)
        for r in journal.read()
    )
    if have_review:
        print("\nreview for today already logged — not duplicating")
        return
    journal.log_review(review)
    LessonStore().save(review, journal.stats())
    print(f"\nlessons.md + review written ({CONFIG.feedback.lessons_path})")


if __name__ == "__main__":
    main()
