"""
Accuracy feedback loop — the journal and the MFE/MAE grader.

The engine is graded *strictly on underlying SPX price action* after the fact:

  * MFE (Maximum Favorable Excursion) — the largest move in the alert's favor
    over the forward window.
  * MAE (Maximum Adverse Excursion) — the largest move against it.

Grading is pure and deterministic (no model in the loop): an alert that showed
real favorable excursion without a large adverse one is ACCURATE; one that only
went against us is INACCURATE; ambiguous is MIXED. Claude reads this graded
history separately (see analyst.review_patterns) to surface *why* — that part is
advisory and logged, never part of the grade.

Storage is an append-only JSONL journal: one `alert` record when it fires, one
`outcome` record when it matures, plus `review` records. Append-only means a
restart can't corrupt history; the price is that in-flight alerts (tracked in
memory) lose their excursion tracking across a restart.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from helenus.config import CONFIG

log = logging.getLogger("helenus.journal")


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class OpenAlert:
    """An alert being tracked for its forward MFE/MAE."""
    id: str
    ts_open: str
    trigger: str
    direction: str                  # "BULLISH" | "BEARISH"
    entry: float
    confidence: float
    context: dict[str, Any]
    high: float = field(init=False)
    low: float = field(init=False)
    last: float = field(init=False)
    early_mae: float = field(init=False)    # adverse excursion within the first N bars
    bars: int = 0

    def __post_init__(self) -> None:
        self.high = self.entry
        self.low = self.entry
        self.last = self.entry
        self.early_mae = 0.0


@dataclass(frozen=True)
class Outcome:
    bars: int
    mfe_pts: float
    mae_pts: float
    mfe_pct: float
    mae_pct: float
    mfe_mae_ratio: float
    net_pts: float                  # signed in the alert's favor (+ = good)
    grade: str                      # ACCURATE | MIXED | INACCURATE
    # Earliness metrics (reported, not part of the grade): the adverse excursion in
    # the FIRST few bars, and whether it tripped the immediate-reversal threshold —
    # the "fired then reversed" failure earliness is meant to drive DOWN.
    early_mae_pts: float = 0.0
    early_reversal: bool = False


def grade_excursion(
    direction: str, entry: float, high: float, low: float, last: float, bars: int,
    early_mae: float = 0.0,
) -> Outcome:
    """
    Pure MFE/MAE grade. Favorable is up for BULLISH, down for BEARISH.

    ACCURATE   : MFE >= mfe_target AND MFE/MAE >= ratio_target AND net_pts >= net_floor
    INACCURATE : MAE >= mae_stop  AND MFE < mfe_target
    MIXED      : everything else

    The net_pts gate keeps a setup whose favorable excursion happened *before or
    around entry* — strong MFE/ratio but a negative held outcome — out of the
    ACCURATE bucket (demoted to MIXED). Without it the feedback loop rewards
    thrusts that had already extended by the time the alert fired.
    """
    cfg = CONFIG.feedback
    if direction == "BULLISH":
        mfe = max(0.0, high - entry)
        mae = max(0.0, entry - low)
        net = last - entry
    else:
        mfe = max(0.0, entry - low)
        mae = max(0.0, high - entry)
        net = entry - last

    ratio = mfe / max(mae, cfg.mae_floor_pts)
    if mfe >= cfg.mfe_target_pts and ratio >= cfg.ratio_target and net >= cfg.net_floor_pts:
        grade = "ACCURATE"
    elif mae >= cfg.mae_stop_pts and mfe < cfg.mfe_target_pts:
        grade = "INACCURATE"
    else:
        grade = "MIXED"

    scale = entry if entry else 1.0
    return Outcome(
        bars=bars,
        mfe_pts=round(mfe, 2),
        mae_pts=round(mae, 2),
        mfe_pct=round(mfe / scale * 100, 3),
        mae_pct=round(mae / scale * 100, 3),
        mfe_mae_ratio=round(ratio, 2),
        net_pts=round(net, 2),
        grade=grade,
        early_mae_pts=round(early_mae, 2),
        early_reversal=early_mae >= cfg.early_reversal_pts,
    )


# --------------------------------------------------------------------------- #
# Outcome tracker (in-memory, stateful)
# --------------------------------------------------------------------------- #

class OutcomeTracker:
    """Rolls MFE/MAE for open alerts and matures them after the forward window."""

    def __init__(self, window: int | None = None, early_window: int | None = None) -> None:
        self.window = window or CONFIG.feedback.forward_window_bars
        self.early_window = early_window or CONFIG.feedback.early_window_bars
        self._open: dict[str, OpenAlert] = {}

    def track(self, alert: OpenAlert) -> None:
        self._open[alert.id] = alert

    @property
    def open_count(self) -> int:
        return len(self._open)

    def update(self, spot: float) -> list[tuple[OpenAlert, Outcome]]:
        """Advance every open alert one bar against `spot`; return any matured."""
        matured: list[tuple[OpenAlert, Outcome]] = []
        for alert in list(self._open.values()):
            alert.high = max(alert.high, spot)
            alert.low = min(alert.low, spot)
            alert.last = spot
            alert.bars += 1
            # Freeze the immediate-reversal read after the first N bars: the adverse
            # excursion so far (high/low are monotonic since entry, so this is the
            # worst adverse move within the early window).
            if alert.bars <= self.early_window:
                adverse = (
                    alert.entry - alert.low if alert.direction == "BULLISH"
                    else alert.high - alert.entry
                )
                alert.early_mae = max(alert.early_mae, max(0.0, adverse))
            if alert.bars >= self.window:
                outcome = grade_excursion(
                    alert.direction, alert.entry, alert.high, alert.low,
                    alert.last, alert.bars, early_mae=alert.early_mae,
                )
                matured.append((alert, outcome))
                del self._open[alert.id]
        return matured


# --------------------------------------------------------------------------- #
# Journal (append-only JSONL)
# --------------------------------------------------------------------------- #

def new_alert_id() -> str:
    return uuid.uuid4().hex[:8]


def _bucket_by_context(
    graded: list[dict[str, Any]], key: str
) -> dict[str, dict[str, Any]]:
    """Bucket graded alerts by a value in their `context` dict (e.g. prior_agreement),
    reporting n / accuracy% / avg MFE / early-reversal% per bucket. Alerts missing the
    key are skipped, so the bucket is empty until the producing feature is enabled."""
    buckets: dict[str, dict[str, Any]] = {}
    agg: dict[str, dict[str, float]] = {}
    for g in graded:
        val = (g.get("context") or {}).get(key)
        if val is None:
            continue
        a = agg.setdefault(val, {"n": 0, "ACCURATE": 0, "early": 0, "mfe": 0.0})
        a["n"] += 1
        if g["grade"] == "ACCURATE":
            a["ACCURATE"] += 1
        if g.get("early_reversal"):
            a["early"] += 1
        a["mfe"] += g.get("mfe_pts", 0.0)
    for val, a in agg.items():
        cnt = int(a["n"])
        buckets[val] = {
            "n": cnt,
            "accuracy_pct": round(a["ACCURATE"] / cnt * 100, 1),
            "avg_mfe_pts": round(a["mfe"] / cnt, 2),
            "early_reversal_rate": round(a["early"] / cnt * 100, 1),
        }
    return buckets


class Journal:
    """Append-only JSONL store of alerts, outcomes, and reviews."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or CONFIG.feedback.journal_path
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _append(self, record: dict[str, Any]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            log.exception("Journal write failed")

    def log_alert(self, alert: OpenAlert) -> None:
        self._append(
            {
                "type": "alert",
                "id": alert.id,
                "ts": alert.ts_open,
                "trigger": alert.trigger,
                "direction": alert.direction,
                "entry": round(alert.entry, 2),
                "confidence": alert.confidence,
                "context": alert.context,
            }
        )

    def log_outcome(
        self, alert_id: str, outcome: Outcome, claude_note: str | None = None
    ) -> None:
        rec = {
            "type": "outcome",
            "alert_id": alert_id,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "bars": outcome.bars,
            "mfe_pts": outcome.mfe_pts,
            "mae_pts": outcome.mae_pts,
            "mfe_pct": outcome.mfe_pct,
            "mae_pct": outcome.mae_pct,
            "mfe_mae_ratio": outcome.mfe_mae_ratio,
            "net_pts": outcome.net_pts,
            "grade": outcome.grade,
            "early_mae_pts": outcome.early_mae_pts,
            "early_reversal": outcome.early_reversal,
        }
        if claude_note:
            rec["claude_note"] = claude_note
        self._append(rec)

    def log_review(self, review: dict[str, Any]) -> None:
        self._append(
            {
                "type": "review",
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                **review,
            }
        )

    # -- reads ------------------------------------------------------------- #

    def read(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        records: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("Skipping malformed journal line")
        return records

    def graded_alerts(self) -> list[dict[str, Any]]:
        """Join alert + outcome records into one row per matured alert."""
        records = self.read()
        alerts = {r["id"]: r for r in records if r.get("type") == "alert"}
        graded: list[dict[str, Any]] = []
        for r in records:
            if r.get("type") != "outcome":
                continue
            alert = alerts.get(r.get("alert_id"))
            if alert is None:
                continue
            graded.append(
                {
                    "id": alert["id"],
                    "ts": alert["ts"],
                    "trigger": alert["trigger"],
                    "direction": alert["direction"],
                    "confidence": alert["confidence"],
                    "context": alert.get("context", {}),
                    "grade": r["grade"],
                    "mfe_pts": r["mfe_pts"],
                    "mae_pts": r["mae_pts"],
                    "mfe_mae_ratio": r["mfe_mae_ratio"],
                    "net_pts": r["net_pts"],
                    # Earliness metrics — absent on pre-existing records (default 0/False).
                    "early_mae_pts": r.get("early_mae_pts", 0.0),
                    "early_reversal": r.get("early_reversal", False),
                }
            )
        return graded

    def stats(self) -> dict[str, Any]:
        """Deterministic accuracy rollup over all graded alerts."""
        graded = self.graded_alerts()
        n = len(graded)
        if n == 0:
            return {"count": 0}

        def avg(key: str) -> float:
            return round(sum(g[key] for g in graded) / n, 2)

        grades = [g["grade"] for g in graded]
        by_trigger: dict[str, dict[str, int]] = {}
        for g in graded:
            t = by_trigger.setdefault(g["trigger"], {"n": 0, "ACCURATE": 0, "early_reversal": 0})
            t["n"] += 1
            if g["grade"] == "ACCURATE":
                t["ACCURATE"] += 1
            if g.get("early_reversal"):
                t["early_reversal"] += 1

        early_reversals = sum(1 for g in graded if g.get("early_reversal"))
        return {
            "count": n,
            "accurate": grades.count("ACCURATE"),
            "mixed": grades.count("MIXED"),
            "inaccurate": grades.count("INACCURATE"),
            "accuracy_pct": round(grades.count("ACCURATE") / n * 100, 1),
            "avg_mfe_pts": avg("mfe_pts"),
            "avg_mae_pts": avg("mae_pts"),
            "avg_ratio": avg("mfe_mae_ratio"),
            # Earliness scorecard: the immediate-reversal rate (drive DOWN) and the
            # avg first-N-bar adverse excursion, alongside MFE-from-entry (avg_mfe_pts).
            "early_reversal_rate": round(early_reversals / n * 100, 1),
            "avg_early_mae_pts": avg("early_mae_pts"),
            "by_trigger": by_trigger,
            # GEX-map validation: does "agrees-with-prior" actually outperform "against"?
            # Bucket the graded alerts by their map context (the Stage-5 measurement) —
            # accuracy %, avg MFE-from-entry, and early-reversal rate per bucket. Empty
            # until the map is enabled and alerts carry the context.
            "by_prior_agreement": _bucket_by_context(graded, "prior_agreement"),
            "by_gex_position": _bucket_by_context(graded, "gex_position_state"),
        }


# --------------------------------------------------------------------------- #
# Lessons — the closed loop back into the analyst prompt
# --------------------------------------------------------------------------- #

def format_lessons(
    review: dict[str, Any], stats: dict[str, Any] | None = None
) -> str:
    """Distil a Claude review into a compact, human-editable lessons doc."""
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n = (stats or {}).get("count")
    acc = (stats or {}).get("accuracy_pct")
    header = f"# Helenus learned lessons\n_Updated {ts}"
    if n is not None:
        header += f" from {n} graded alerts ({acc}% accurate)"
    header += "._\n"

    def section(title: str, items: list[str]) -> str:
        if not items:
            return ""
        body = "\n".join(f"- {x}" for x in items)
        return f"\n## {title}\n{body}\n"

    out = header
    if review.get("summary"):
        out += f"\n{review['summary']}\n"
    out += section("What works (favor these)", review.get("accurate_patterns", []))
    out += section("What misses (be skeptical)", review.get("inaccurate_patterns", []))
    out += section("Tuning notes", review.get("suggestions", []))
    return out.strip() + "\n"


class LessonStore:
    """
    Persists the latest distilled lessons to a markdown file. The analyst loads
    this back into its system prompt, so graded outcomes feed forward into future
    judgments. The file is plain markdown — safe to hand-edit and curate.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or CONFIG.feedback.lessons_path
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def save(self, review: dict[str, Any], stats: dict[str, Any] | None = None) -> str:
        text = format_lessons(review, stats)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            log.exception("Lessons write failed")
        return text

    def load(self) -> str:
        if not os.path.exists(self.path):
            return ""
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            log.exception("Lessons read failed")
            return ""
