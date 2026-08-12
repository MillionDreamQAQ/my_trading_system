from __future__ import annotations

import unittest

import pandas as pd

from gold_research.market_calendar import unexpected_missing_bar_ranges


class OandaXauUsdCalendarTests(unittest.TestCase):
    def _ranges(self, previous: str, current: str):
        return unexpected_missing_bar_ranges(
            pd.Timestamp(previous),
            pd.Timestamp(current),
            pd.Timedelta("1min"),
            market_calendar="oanda_xau_usd",
            closed_weekdays=(5,),
        )

    def test_summer_daily_maintenance_gap_is_not_missing_data(self) -> None:
        self.assertEqual(self._ranges("2026-08-10T20:59:00Z", "2026-08-10T22:04:00Z"), [])

    def test_winter_daily_maintenance_gap_moves_with_new_york_time(self) -> None:
        self.assertEqual(self._ranges("2026-12-07T21:59:00Z", "2026-12-07T23:04:00Z"), [])

    def test_weekend_gap_is_not_missing_data(self) -> None:
        self.assertEqual(self._ranges("2026-08-07T20:59:00Z", "2026-08-09T22:04:00Z"), [])

    def test_independence_day_observed_early_close_is_not_missing_data(self) -> None:
        # Independence Day 2026 falls on Saturday, so its Friday observance
        # closes OANDA XAU_USD at 13:00 New York time (17:00 UTC).
        self.assertEqual(self._ranges("2026-07-03T16:59:00Z", "2026-07-03T21:00:00Z"), [])

    def test_independence_day_observed_keeps_pre_close_gaps_visible(self) -> None:
        ranges = self._ranges("2026-07-03T15:59:00Z", "2026-07-03T16:02:00Z")

        self.assertEqual(
            ranges,
            [(pd.Timestamp("2026-07-03T16:00:00Z"), pd.Timestamp("2026-07-03T16:02:00Z"), 2)],
        )

    def test_gap_after_maintenance_is_still_reported(self) -> None:
        ranges = self._ranges("2026-08-10T20:59:00Z", "2026-08-10T22:06:00Z")

        self.assertEqual(
            ranges,
            [(pd.Timestamp("2026-08-10T22:04:00Z"), pd.Timestamp("2026-08-10T22:06:00Z"), 2)],
        )

    def test_open_market_gap_is_still_reported(self) -> None:
        ranges = self._ranges("2026-08-10T12:00:00Z", "2026-08-10T12:03:00Z")

        self.assertEqual(
            ranges,
            [(pd.Timestamp("2026-08-10T12:01:00Z"), pd.Timestamp("2026-08-10T12:03:00Z"), 2)],
        )


if __name__ == "__main__":
    unittest.main()
