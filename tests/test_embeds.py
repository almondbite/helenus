"""
Offline tests for the Discord embed builders — specifically that a verbose Claude
verdict can't produce an embed that violates Discord's length limits (which 400s
the send and, before the fix, killed bar_worker).

    pytest tests/test_embeds.py
    python tests/test_embeds.py
"""

from __future__ import annotations

from helenus.engine.scan2 import Direction, KeyLevel, Signal, TriggerType
from helenus.output import embeds

# Discord limits.
FIELD_MAX = 1024
DESC_MAX = 4096
TITLE_MAX = 256
TOTAL_MAX = 6000


def _assert_within_limits(embed) -> None:
    assert len(embed.title or "") <= TITLE_MAX
    assert len(embed.description or "") <= DESC_MAX
    for f in embed.fields:
        assert len(f.name or "") <= 256, f"field name too long: {f.name!r}"
        assert 1 <= len(f.value or "") <= FIELD_MAX, f"field value bad len: {f.name!r}"
    assert len(embed) <= TOTAL_MAX  # discord.Embed.__len__ = total chars


def _long_signal() -> Signal:
    thesis = "This is a deliberately enormous thesis. " * 80  # ~3200 chars
    risks = ["A risk flag that is itself quite wordy and detailed. " * 5 for _ in range(6)]
    return Signal(
        trigger=TriggerType.LEVEL_REJECTION,
        direction=Direction.BEARISH,
        level=KeyLevel(7490.0, "Round 7490", 0.6),
        spot=7487.57,
        volume_ratio=2.5,
        confidence=66.0,
        trend_label="BEARISH WITH TREND",
        notes=[thesis] + [f"⚠ {r}" for r in risks],
    )


def test_signal_embed_clips_a_huge_verdict() -> None:
    embed = embeds.signal_embed(_long_signal())
    _assert_within_limits(embed)
    # The read goes in the description (4096), not a 1024 field.
    assert embed.description and len(embed.description) > 1024


def test_premarket_embed_clips() -> None:
    embed = embeds.premarket_embed(_long_signal(), macro_lines=["/ES `5000`"])
    _assert_within_limits(embed)


def test_review_embed_clips_long_bullets() -> None:
    review = {
        "summary": "Summary paragraph. " * 300,  # ~5700 chars, over 4096
        "accurate_patterns": ["A long accurate pattern note. " * 40 for _ in range(5)],
        "inaccurate_patterns": ["A long miss note. " * 40 for _ in range(5)],
        "suggestions": ["A long tuning suggestion. " * 40 for _ in range(5)],
    }
    stats = {"count": 2, "accuracy_pct": 50.0, "accurate": 1, "mixed": 1,
             "inaccurate": 0, "avg_mfe_pts": 15.0, "avg_mae_pts": 3.5, "avg_ratio": 4.3}
    embed = embeds.review_embed(review, stats)
    _assert_within_limits(embed)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
