from __future__ import annotations

import unittest

import pandas as pd

from gold_research.config import ResearchConfig
from gold_research.domain import Direction
from gold_research.strategy.entry_point_2 import detect_entry_point_2


def _config(direction: str = "both") -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {
                "symbol": "XAUUSD",
                "provider": "test",
                "price_basis": "mid",
                "timezone": "UTC",
            },
            "timeframes": {"base": "15min", "medium": "1h", "large": "4h", "timezone": "UTC"},
            "strategy": {"direction": direction},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {"enabled": True, "pullback_min_atr": 0.5, "pullback_min_bars": 2, "max_setup_bars": 30},
            "risk": {"atr_period": 2, "stop_atr": 2.0, "target_atr": 4.0, "max_hold_bars": 80},
            "costs": {
                "spread_model": "fixed",
                "spread_value": 0.0,
                "slippage_model": "fixed",
                "slippage_value": 0.0,
                "commission_per_unit": 0.0,
                "require_explicit_costs": True,
            },
        }
    )


def _context(closes: list[float]) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": timestamps,
            "close_time": timestamps + pd.Timedelta("15min"),
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "signal_time": timestamps + pd.Timedelta("15min"),
            "base_trend": ["up"] * len(closes),
            "medium_trend": ["up"] * len(closes),
            "large_trend": ["up"] * len(closes),
            "all_up": [True] * len(closes),
            "all_down": [False] * len(closes),
            "atr": [1.0] * len(closes),
        }
    )


class EntryPoint2Tests(unittest.TestCase):
    def test_new_close_breakout_emits_next_bar_open(self) -> None:
        context = _context([100, 100, 100, 100, 101.0, 101.2, 100.8])

        signals = detect_entry_point_2(context, _config())

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, Direction.LONG)
        self.assertEqual(signals[0].signal_time, context.loc[4, "signal_time"])
        self.assertEqual(signals[0].entry_time, context.loc[5, "open_time"])
        self.assertEqual(signals[0].reason, "fresh_close_breakout_above_3_bar_high")

    def test_staying_above_breakout_does_not_repeat(self) -> None:
        context = _context([100, 100, 100, 100, 101.0, 101.2, 101.4, 101.6])

        signals = detect_entry_point_2(context, _config())

        self.assertEqual(len(signals), 1)

    def test_monotonic_new_highs_do_not_emit_a_signal_on_every_bar(self) -> None:
        context = _context([100, 100, 100, 100, 101, 102, 103, 104, 105])

        signals = detect_entry_point_2(context, _config())

        self.assertEqual(len(signals), 1)

    def test_short_is_mirror_and_direction_filter_is_honored(self) -> None:
        context = _context([100, 100, 100, 100, 99.0, 98.8, 99.2])
        context["base_trend"] = "down"
        context["medium_trend"] = "down"
        context["large_trend"] = "down"
        context["all_up"] = False
        context["all_down"] = True

        signals = detect_entry_point_2(context, _config("short"))

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].side, Direction.SHORT)
        self.assertEqual(detect_entry_point_2(context, _config("long")), [])


if __name__ == "__main__":
    unittest.main()
