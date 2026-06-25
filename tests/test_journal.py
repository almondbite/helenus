"""
Offline tests for journal.py — MFE/MAE grading, the outcome tracker, and the
append-only journal round-trip. No network, no Schwab key.

    pytest tests/test_journal.py
    python tests/test_journal.py

Grades assume FeedbackConfig defaults: mfe_target=5, mae_stop=5, ratio_target=1.5.
"""

from __future__ import annotations

import os
import tempfile

from helenus.journal import (
    Journal,
    LessonStore,
    OpenAlert,
    OutcomeTracker,
    format_lessons,
    grade_excursion,
    new_alert_id,
)


# --------------------------------------------------------------------------- #
# Deterministic MFE/MAE grading
# --------------------------------------------------------------------------- #

def test_bullish_accurate() -> None:
    # Up 12 favorable, only 2 adverse -> strong favorable excursion.
    o = grade_excursion("BULLISH", entry=5000, high=5012, low=4998, last=5010, bars=30)
    assert o.mfe_pts == 12.0
    assert o.mae_pts == 2.0
    assert o.mfe_mae_ratio == 6.0
    assert o.net_pts == 10.0
    assert o.grade == "ACCURATE"


def test_bearish_accurate() -> None:
    # Favorable is DOWN for a bearish call: low 4988 -> MFE 12, high 5002 -> MAE 2.
    o = grade_excursion("BEARISH", entry=5000, high=5002, low=4988, last=4990, bars=30)
    assert o.mfe_pts == 12.0
    assert o.mae_pts == 2.0
    assert o.net_pts == 10.0
    assert o.grade == "ACCURATE"


def test_inaccurate() -> None:
    # Barely any favorable move, big adverse excursion.
    o = grade_excursion("BULLISH", entry=5000, high=5001, low=4992, last=4993, bars=30)
    assert o.mfe_pts == 1.0
    assert o.mae_pts == 8.0
    assert o.grade == "INACCURATE"


def test_mixed() -> None:
    # Favorable below target AND adverse below stop -> ambiguous.
    o = grade_excursion("BULLISH", entry=5000, high=5003, low=4998, last=5001, bars=30)
    assert o.mfe_pts == 3.0
    assert o.mae_pts == 2.0
    assert o.grade == "MIXED"


def test_strong_excursion_but_negative_net_is_demoted() -> None:
    # Big favorable MFE and a fine ratio, but the move came before/around entry
    # and the held outcome is negative -> not ACCURATE, demoted to MIXED.
    o = grade_excursion("BULLISH", entry=5000, high=5012, low=4998, last=4997, bars=30)
    assert o.mfe_pts == 12.0
    assert o.mfe_mae_ratio == 6.0              # 12 / 2, clears ratio_target
    assert o.net_pts == -3.0
    assert o.grade == "MIXED"


def test_ratio_floor_no_div_by_zero() -> None:
    # No adverse excursion at all; ratio must stay finite via the mae floor.
    o = grade_excursion("BULLISH", entry=5000, high=5006, low=5000, last=5006, bars=30)
    assert o.mae_pts == 0.0
    assert o.mfe_mae_ratio == 6.0 / 0.25      # mfe / mae_floor
    assert o.grade == "ACCURATE"


def test_pct_fields() -> None:
    o = grade_excursion("BULLISH", entry=5000, high=5050, low=5000, last=5050, bars=30)
    assert o.mfe_pct == 1.0                    # 50 / 5000 * 100
    assert o.mae_pct == 0.0


# --------------------------------------------------------------------------- #
# Outcome tracker
# --------------------------------------------------------------------------- #

def _alert(direction: str = "BULLISH", entry: float = 5000.0) -> OpenAlert:
    return OpenAlert(
        id=new_alert_id(),
        ts_open="2026-06-19T10:00:00-04:00",
        trigger="Vanna Rally",
        direction=direction,
        entry=entry,
        confidence=70.0,
        context={"regime": "NEGATIVE_GAMMA", "vanna_active": True},
    )


def test_tracker_matures_after_window() -> None:
    tracker = OutcomeTracker(window=3)
    tracker.track(_alert())
    assert tracker.update(5005) == []          # bar 1
    assert tracker.update(4995) == []          # bar 2
    matured = tracker.update(5010)             # bar 3 -> matures
    assert len(matured) == 1
    assert tracker.open_count == 0

    alert, outcome = matured[0]
    assert outcome.bars == 3
    assert outcome.mfe_pts == 10.0             # high 5010 - entry 5000
    assert outcome.mae_pts == 5.0              # entry 5000 - low 4995
    assert outcome.grade == "ACCURATE"         # ratio 2.0, mfe 10


def test_tracker_handles_multiple_alerts() -> None:
    tracker = OutcomeTracker(window=2)
    tracker.track(_alert("BULLISH"))
    tracker.track(_alert("BEARISH"))
    assert tracker.open_count == 2
    tracker.update(5003)
    matured = tracker.update(4990)
    assert len(matured) == 2
    assert tracker.open_count == 0


# --------------------------------------------------------------------------- #
# Journal round-trip
# --------------------------------------------------------------------------- #

def test_journal_roundtrip_and_stats() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "journal.jsonl")   # parent auto-created
        j = Journal(path=path)

        a1 = _alert("BULLISH")
        j.log_alert(a1)
        j.log_outcome(
            a1.id,
            grade_excursion("BULLISH", 5000, 5012, 4998, 5010, 30),  # ACCURATE
        )
        a2 = _alert("BULLISH")
        j.log_alert(a2)
        j.log_outcome(
            a2.id,
            grade_excursion("BULLISH", 5000, 5001, 4992, 4993, 30),  # INACCURATE
            claude_note="counter-trend cross stalled into a call wall",
        )

        assert len(j.read()) == 4              # 2 alerts + 2 outcomes
        graded = j.graded_alerts()
        assert len(graded) == 2
        assert {g["grade"] for g in graded} == {"ACCURATE", "INACCURATE"}

        s = j.stats()
        assert s["count"] == 2
        assert s["accurate"] == 1
        assert s["inaccurate"] == 1
        assert s["accuracy_pct"] == 50.0
        assert s["by_trigger"]["Vanna Rally"] == {"n": 2, "ACCURATE": 1, "early_reversal": 0}


def test_stats_empty() -> None:
    with tempfile.TemporaryDirectory() as d:
        j = Journal(path=os.path.join(d, "empty.jsonl"))
        assert j.stats() == {"count": 0}
        assert j.graded_alerts() == []


# --------------------------------------------------------------------------- #
# Earliness metrics — immediate-reversal rate + first-N-bar adverse excursion
# --------------------------------------------------------------------------- #

def test_early_reversal_flagged_on_large_first_bar_adverse() -> None:
    # Strong eventual MFE, but a big adverse move in the first N bars → flagged
    # (the "fired then reversed" earliness failure). The grade itself is unchanged.
    o = grade_excursion("BULLISH", 5000, 5012, 4998, 5010, 30, early_mae=4.0)
    assert o.early_mae_pts == 4.0
    assert o.early_reversal is True            # 4.0 >= early_reversal_pts (3.0)
    assert o.grade == "ACCURATE"               # grade buckets untouched


def test_no_early_reversal_when_first_bars_are_clean() -> None:
    o = grade_excursion("BULLISH", 5000, 5012, 4998, 5010, 30, early_mae=1.0)
    assert o.early_reversal is False


def test_early_mae_freezes_after_the_early_window() -> None:
    tracker = OutcomeTracker(window=6, early_window=2)
    tracker.track(_alert("BULLISH"))           # entry 5000
    tracker.update(4996)                        # bar1: adverse 4 → early_mae 4
    tracker.update(5000)                        # bar2: still within window, adverse 0
    tracker.update(4990)                        # bar3: beyond window — must NOT update
    tracker.update(5001)
    tracker.update(5002)
    matured = tracker.update(5003)              # bar6 → matures
    _alert_obj, outcome = matured[0]
    assert outcome.early_mae_pts == 4.0         # frozen at the first-2-bar adverse
    assert outcome.mae_pts == 10.0              # full-window MAE still sees the 4990 low
    assert outcome.early_reversal is True


def test_stats_reports_early_reversal_rate() -> None:
    with tempfile.TemporaryDirectory() as d:
        j = Journal(path=os.path.join(d, "j.jsonl"))
        a1 = _alert(); j.log_alert(a1)
        j.log_outcome(a1.id, grade_excursion("BULLISH", 5000, 5012, 4998, 5010, 30, early_mae=5.0))
        a2 = _alert(); j.log_alert(a2)
        j.log_outcome(a2.id, grade_excursion("BULLISH", 5000, 5012, 4998, 5010, 30, early_mae=0.0))
        s = j.stats()
        assert s["early_reversal_rate"] == 50.0
        assert "avg_early_mae_pts" in s


# --------------------------------------------------------------------------- #
# Lessons (closed loop back into the analyst)
# --------------------------------------------------------------------------- #

_REVIEW = {
    "summary": "Vanna alerts in negative gamma follow through; counter-trend fades.",
    "accurate_patterns": ["Active vanna in NEGATIVE_GAMMA after a falling tape"],
    "inaccurate_patterns": ["Counter-trend level cross into a call wall"],
    "suggestions": ["Down-weight counter-trend crosses near call walls"],
}


def test_format_lessons_contains_sections() -> None:
    text = format_lessons(_REVIEW, {"count": 12, "accuracy_pct": 58.3})
    assert "Helenus learned lessons" in text
    assert "12 graded alerts (58.3% accurate)" in text
    assert "What works" in text and "What misses" in text and "Tuning notes" in text
    assert "Active vanna in NEGATIVE_GAMMA" in text


def test_lesson_store_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = LessonStore(path=os.path.join(d, "sub", "lessons.md"))
        assert store.load() == ""                 # nothing yet
        saved = store.save(_REVIEW, {"count": 3, "accuracy_pct": 66.7})
        loaded = store.load()
        assert loaded == saved.strip()
        assert "Counter-trend level cross" in loaded


def test_format_lessons_skips_empty_sections() -> None:
    text = format_lessons({"summary": "n/a", "accurate_patterns": []})
    assert "What works" not in text              # empty list -> section omitted
    assert "n/a" in text


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
