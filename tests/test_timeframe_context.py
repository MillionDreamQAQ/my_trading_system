from __future__ import annotations

import unittest

import pandas as pd

from gold_research.config import TrendConfig
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.data.resample import resample_bars
from gold_research.domain import InstrumentMetadata, PriceBasis
from gold_research.strategy.timeframe_context import build_timeframe_context


def _series(start: str, periods: int, freq: str, values: list[float]):
    timestamps = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": values,
            "high": [value + 1 for value in values],
            "low": [value - 1 for value in values],
            "close": values,
        }
    )
    return normalize_ohlc_frame(
        frame,
        InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
        freq,
    )


class TimeframeContextTests(unittest.TestCase):
    def test_higher_timeframe_state_is_not_available_before_close(self) -> None:
        base = _series("2026-01-01 00:00", 20, "15min", [100 + i for i in range(20)])
        medium = resample_bars(base, "1h")
        large = resample_bars(base, "4h")

        context = build_timeframe_context(
            base,
            medium,
            large,
            TrendConfig(ema_fast=2, ema_slow=3, slope_lookback=1),
        )

        before_first_hour_close = context[context["signal_time"] < pd.Timestamp("2026-01-01 01:00", tz="UTC")]
        self.assertTrue((before_first_hour_close["medium_trend"] == "unknown").all())
        aligned = context[["medium_source_close_time", "signal_time"]].dropna()
        self.assertTrue((aligned["medium_source_close_time"] <= aligned["signal_time"]).all())

    def test_future_tail_changes_do_not_change_prefix_context(self) -> None:
        values = [100 + i for i in range(40)]
        base = _series("2026-01-01", 40, "15min", values)
        medium = resample_bars(base, "1h")
        large = resample_bars(base, "4h")
        config = TrendConfig(ema_fast=2, ema_slow=3, slope_lookback=1)
        first = build_timeframe_context(base, medium, large, config)

        changed = base.copy()
        changed.bars.loc[30:, ["open", "high", "low", "close"]] += 1000
        changed_medium = resample_bars(changed, "1h")
        changed_large = resample_bars(changed, "4h")
        second = build_timeframe_context(changed, changed_medium, changed_large, config)

        prefix = first["signal_time"] < pd.Timestamp("2026-01-01 07:30", tz="UTC")
        columns = ["base_trend", "medium_trend", "large_trend", "medium_source_close_time", "large_source_close_time"]
        self.assertTrue(first.loc[prefix, columns].equals(second.loc[prefix, columns]))


if __name__ == "__main__":
    unittest.main()
