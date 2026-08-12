from __future__ import annotations

import unittest

import pandas as pd

from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.data.resample import resample_bars
from gold_research.domain import InstrumentMetadata, PriceBasis


class ResamplingTests(unittest.TestCase):
    def test_only_complete_utc_hour_bars_are_emitted(self) -> None:
        timestamps = pd.date_range("2026-01-01 00:00", periods=9, freq="15min", tz="UTC")
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": range(100, 109),
                "high": range(101, 110),
                "low": range(99, 108),
                "close": range(100, 109),
            }
        )
        source = normalize_ohlc_frame(
            frame,
            InstrumentMetadata(
                provider="test-provider",
                symbol="XAU_USD",
                price_basis=PriceBasis.MID,
                source_interval="15min",
            ),
            "15min",
        )

        hourly = resample_bars(source, "1h")

        self.assertEqual(len(hourly.bars), 2)
        self.assertEqual(hourly.bars["open_time"].tolist(), list(pd.date_range("2026-01-01", periods=2, freq="1h", tz="UTC")))
        self.assertEqual(hourly.bars.iloc[0]["open"], 100)
        self.assertEqual(hourly.bars.iloc[0]["high"], 104)
        self.assertEqual(hourly.bars.iloc[0]["low"], 99)
        self.assertEqual(hourly.bars.iloc[0]["close"], 103)

    def test_utc_boundary_does_not_depend_on_local_timezone(self) -> None:
        timestamps = pd.date_range("2026-01-01 23:00", periods=8, freq="15min", tz="UTC")
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [10] * 8,
                "high": [12] * 8,
                "low": [9] * 8,
                "close": [11] * 8,
            }
        )
        source = normalize_ohlc_frame(
            frame,
            InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
            "15min",
        )

        hourly = resample_bars(source, "1h")

        self.assertEqual(hourly.bars["open_time"].tolist(), list(pd.date_range("2026-01-01 23:00", periods=2, freq="1h", tz="UTC")))


if __name__ == "__main__":
    unittest.main()
