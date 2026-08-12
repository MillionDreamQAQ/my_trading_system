from __future__ import annotations

import unittest

import pandas as pd

from gold_research.config import ResearchConfig
from gold_research.domain import Direction
from gold_research.strategy.entry_point_3 import detect_entry_point_3
from gold_research.strategy.state_machine import SetupState


def _config(
    direction: str = "both",
    *,
    base_timeframe: str = "15min",
) -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {"symbol": "XAUUSD", "provider": "test", "price_basis": "mid", "timezone": "UTC"},
            "timeframes": {"base": base_timeframe, "medium": "1h", "large": "4h", "timezone": "UTC"},
            "strategy": {"direction": direction},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {"enabled": True, "pullback_min_atr": 0.5, "pullback_min_bars": 2, "max_setup_bars": 6},
            "risk": {"atr_period": 2, "stop_atr": 2.0, "target_atr": 4.0, "max_hold_bars": 80},
            "costs": {
                "spread_model": "fixed", "spread_value": 0.0,
                "slippage_model": "fixed", "slippage_value": 0.0,
                "commission_per_unit": 0.0, "require_explicit_costs": True,
            },
        }
    )


def _context(closes: list[float], atr: float = 2.0) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": timestamps,
            "close_time": timestamps + pd.Timedelta("15min"),
            "open": closes,
            "high": [value + 0.1 for value in closes],
            "low": [value - 0.1 for value in closes],
            "close": closes,
            "signal_time": timestamps + pd.Timedelta("15min"),
            "base_trend": ["up"] * len(closes), "medium_trend": ["up"] * len(closes), "large_trend": ["up"] * len(closes),
            "all_up": [True] * len(closes), "all_down": [False] * len(closes), "atr": [atr] * len(closes),
        }
    )


class EntryPoint3Tests(unittest.TestCase):
    def test_initial_breakout_only_creates_setup(self) -> None:
        result = detect_entry_point_3(_context([100, 100, 100, 100, 101]), _config())

        self.assertEqual(result.signals, ())
        self.assertEqual(len(result.setups), 1)
        self.assertEqual(result.setups[0].state, SetupState.CANCELLED)
        self.assertEqual(result.setups[0].cancel_reason, "data_ended_before_setup_completion")

    def test_valid_pullback_and_rebreakout_emits_once(self) -> None:
        context = _context([100, 100, 100, 100, 101, 103, 101.8, 101.5, 102.5, 103.5, 104])
        result = detect_entry_point_3(context, _config())

        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].side, Direction.LONG)
        self.assertEqual(result.signals[0].reason, "pullback_rebreakout_above_pullback_high")
        self.assertEqual(result.signals[0].entry_time, context.loc[9, "open_time"])
        self.assertEqual(result.setups[0].state, SetupState.TRIGGERED)

    def test_breakout_level_failure_cancels_setup(self) -> None:
        result = detect_entry_point_3(_context([100, 100, 100, 100, 101, 100.0]), _config())

        self.assertEqual(result.signals, ())
        self.assertEqual(result.setups[0].cancel_reason, "close_below_initial_breakout")

    def test_oanda_returned_gap_does_not_cancel_an_active_setup(self) -> None:
        context = _context([100, 100, 100, 100, 101, 103, 101.8, 101.5, 102.5])
        context.loc[6:, ["open_time", "close_time", "signal_time"]] += pd.Timedelta(hours=1)

        result = detect_entry_point_3(context, _config())

        self.assertEqual(len(result.signals), 1)
        self.assertNotIn("data_gap", [setup.cancel_reason for setup in result.setups])

    def test_short_is_mirror(self) -> None:
        context = _context([100, 100, 100, 100, 99, 97, 98.2, 98.5, 97.5, 96.5, 96])
        context["all_up"] = False
        context["all_down"] = True
        context["base_trend"] = "down"
        context["medium_trend"] = "down"
        context["large_trend"] = "down"
        result = detect_entry_point_3(context, _config("short"))

        self.assertEqual(len(result.signals), 1)
        self.assertEqual(result.signals[0].side, Direction.SHORT)


if __name__ == "__main__":
    unittest.main()
