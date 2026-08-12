from __future__ import annotations

import unittest

import pandas as pd

from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.data.validate import DataValidationError, validate_bar_series
from gold_research.domain import InstrumentMetadata, PriceBasis


def metadata(timezone: str = "UTC") -> InstrumentMetadata:
    return InstrumentMetadata(
        provider="test-provider",
        symbol="XAUUSD",
        price_basis=PriceBasis.MID,
        source_timezone=timezone,
        source_interval="15min",
    )


class DataLoadingTests(unittest.TestCase):
    def test_naive_timestamps_use_source_timezone_and_become_utc(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2026-01-01 08:00", "2026-01-01 08:15"],
                "open": [100, 101],
                "high": [102, 103],
                "low": [99, 100],
                "close": [101, 102],
            }
        )

        result = normalize_ohlc_frame(frame, metadata("Asia/Shanghai"), "15min")

        self.assertEqual(result.bars["open_time"].iloc[0], pd.Timestamp("2026-01-01 00:00", tz="UTC"))
        self.assertEqual(result.bars["close_time"].iloc[1], pd.Timestamp("2026-01-01 00:30", tz="UTC"))

    def test_invalid_ohlc_is_rejected_with_stable_code(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": ["2026-01-01T00:00:00Z"],
                "open": [100],
                "high": [99],
                "low": [98],
                "close": [100],
            }
        )
        series = normalize_ohlc_frame(frame, metadata(), "15min")

        with self.assertRaises(DataValidationError) as context:
            validate_bar_series(series, expected_interval="15min")

        self.assertIn("INVALID_OHLC", {issue.code for issue in context.exception.issues})

    def test_missing_bar_is_reported_without_forward_fill(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": [
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:15:00Z",
                    "2026-01-01T00:45:00Z",
                ],
                "open": [100, 101, 103],
                "high": [102, 103, 105],
                "low": [99, 100, 102],
                "close": [101, 102, 104],
            }
        )
        series = normalize_ohlc_frame(frame, metadata(), "15min")

        with self.assertRaises(DataValidationError) as context:
            validate_bar_series(series, expected_interval="15min")

        self.assertEqual(len(series.bars), 3)
        gaps = [issue for issue in context.exception.issues if issue.code == "MISSING_BARS"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].count, 1)


if __name__ == "__main__":
    unittest.main()

