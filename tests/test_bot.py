"""
Offline tests for the pure helpers in bot.py. No Discord, no network.

    pytest tests/test_bot.py
    python tests/test_bot.py
"""

from __future__ import annotations

from helenus.bot import _candles_to_bars
from helenus.engine.scan2 import MarketState


def _spx(ts_min: int, o: float, h: float, l: float, c: float) -> dict:
    return {"datetime": ts_min * 60_000, "open": o, "high": h, "low": l, "close": c}


def _spy(ts_min: int, vol: float) -> dict:
    return {"datetime": ts_min * 60_000, "volume": vol}


def test_merge_aligns_spy_volume_to_spx_price() -> None:
    spx = [_spx(1000, 7500, 7530, 7498, 7508), _spx(1001, 7508, 7512, 7460, 7475)]
    spy = [_spy(1000, 1_000_000), _spy(1001, 2_500_000)]
    bars = _candles_to_bars(spx, spy)
    assert [b.close for b in bars] == [7508.0, 7475.0]
    assert [b.volume for b in bars] == [1_000_000.0, 2_500_000.0]


def test_backfill_sets_session_extremes() -> None:
    spx = [_spx(1000, 7500, 7530, 7498, 7508), _spx(1001, 7508, 7512, 7460, 7475)]
    bars = _candles_to_bars(spx, [_spy(1000, 1), _spy(1001, 1)])
    st = MarketState()
    for b in bars:
        st.push_bar(b)
    assert st.session_high == 7530.0
    assert st.session_low == 7460.0


def test_missing_spy_minute_is_zero_volume_not_a_crash() -> None:
    bars = _candles_to_bars([_spx(9, 1.0, 2.0, 0.5, 1.5)], [])
    assert bars[0].volume == 0.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
