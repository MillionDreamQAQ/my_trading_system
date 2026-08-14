from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from gold_research.backtest.execution import _last_swing_extreme, run_backtest
from gold_research.config import ResearchConfig
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import Direction, InstrumentMetadata, PriceBasis, Signal


def _config(
    *,
    structural_stop_enabled: bool = False,
    structural_stop_buffer_atr: float = 0.5,
    swing_lookback: int = 20,
    breakeven_enabled: bool = False,
    breakeven_trigger_atr: float = 1.5,
) -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {
                "symbol": "XAUUSD",
                "provider": "test",
                "price_basis": "mid",
                "timezone": "UTC",
                "point_value": 1.0,
            },
            "timeframes": {"base": "15min", "medium": "1h", "large": "4h", "timezone": "UTC"},
            "strategy": {"direction": "both"},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {
                "enabled": False,
                "pullback_min_atr": 0.5,
                "pullback_min_bars": 2,
                "max_setup_bars": 30,
            },
            "risk": {
                "atr_period": 2,
                "stop_atr": 2.0,
                "target_atr": 4.0,
                "max_hold_bars": 20,
                "structural_stop_enabled": structural_stop_enabled,
                "structural_stop_buffer_atr": structural_stop_buffer_atr,
                "swing_lookback": swing_lookback,
                "breakeven_enabled": breakeven_enabled,
                "breakeven_trigger_atr": breakeven_trigger_atr,
            },
            "costs": {
                "spread_model": "fixed",
                "spread_value": 0.0,
                "slippage_model": "fixed",
                "slippage_value": 0.0,
                "commission_per_unit": 0.0,
                "require_explicit_costs": True,
            },
            "position": {"lots": 1.0, "units_per_lot": 1.0, "leverage": 1.0},
        }
    )


def _bars(rows: list[tuple[float, float, float, float]]):
    timestamps = pd.date_range("2026-01-01", periods=len(rows), freq="15min", tz="UTC")
    return normalize_ohlc_frame(
        pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [row[0] for row in rows],
                "high": [row[1] for row in rows],
                "low": [row[2] for row in rows],
                "close": [row[3] for row in rows],
            }
        ),
        InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
        "15min",
    )


def _signal(series, index: int, side: Direction, atr: float = 2.0) -> Signal:
    signal_time = series.bars.loc[index, "close_time"]
    entry_time = series.bars.loc[index + 1, "open_time"] if index + 1 < len(series.bars) else None
    return Signal(
        strategy_id="entry_point_2",
        side=side,
        signal_time=signal_time,
        entry_time=entry_time,
        breakout_level=100.0,
        atr=atr,
        reason="test",
        base_trend="up" if side is Direction.LONG else "down",
        medium_trend="up" if side is Direction.LONG else "down",
        large_trend="up" if side is Direction.LONG else "down",
    )


class DirectionTwoTests(unittest.TestCase):
    def test_swing_lookup_ignores_non_numeric_values(self) -> None:
        values = np.array([None, "missing", 10.0, np.nan, 8.0, 9.0], dtype=object)

        self.assertEqual(_last_swing_extreme(values, 5, 20, Direction.LONG), 8.0)

    def test_long_uses_last_confirmed_swing_low_for_structural_stop(self) -> None:
        bars = _bars(
            [
                (100, 101, 99, 100),
                (99, 100, 98, 99),
                (98, 99, 95, 96),
                (96, 98, 96.5, 97),
                (99, 105, 98, 104),
                (100, 101, 99, 100),
                (100, 101, 93, 99),
            ]
        )

        result = run_backtest(
            bars,
            [_signal(bars, 4, Direction.LONG)],
            _config(structural_stop_enabled=True, swing_lookback=10),
        )

        trade = result.trades[0]
        self.assertEqual(trade.stop_price, 94.0)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertEqual(trade.exit_price, 94.0)
        self.assertEqual(trade.r_multiple, -1.0)

    def test_short_uses_symmetric_swing_high_structural_stop(self) -> None:
        bars = _bars(
            [
                (100, 101, 99, 100),
                (101, 102, 100, 101),
                (104, 105, 103, 104),
                (103, 104, 101, 102),
                (99, 102, 95, 96),
                (100, 101, 99, 100),
                (100, 107, 99, 101),
            ]
        )

        result = run_backtest(
            bars,
            [_signal(bars, 4, Direction.SHORT)],
            _config(structural_stop_enabled=True, swing_lookback=10),
        )

        trade = result.trades[0]
        self.assertEqual(trade.stop_price, 106.0)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertEqual(trade.exit_price, 106.0)
        self.assertEqual(trade.r_multiple, -1.0)

    def test_breakeven_moves_stop_after_trigger_bar_for_next_bar(self) -> None:
        bars = _bars(
            [
                (100, 100.5, 99.5, 100),
                (100, 102.5, 99.5, 101),
                (100, 103.5, 99.0, 102),
                (99, 102, 98, 99),
            ]
        )

        result = run_backtest(
            bars,
            [_signal(bars, 0, Direction.LONG)],
            _config(breakeven_enabled=True, breakeven_trigger_atr=1.5),
        )

        trade = result.trades[0]
        self.assertEqual(trade.stop_price, 100.0)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertEqual(trade.exit_price, 99.0)
        self.assertAlmostEqual(trade.r_multiple, -0.25)

    def test_breakeven_is_symmetric_for_short_positions(self) -> None:
        bars = _bars(
            [
                (100, 100.5, 99.5, 100),
                (100, 100.5, 97.5, 99),
                (100, 100.5, 96.5, 98),
                (101, 102, 100, 101),
            ]
        )

        result = run_backtest(
            bars,
            [_signal(bars, 0, Direction.SHORT)],
            _config(breakeven_enabled=True, breakeven_trigger_atr=1.5),
        )

        trade = result.trades[0]
        self.assertEqual(trade.stop_price, 100.0)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertEqual(trade.exit_price, 101.0)
        self.assertAlmostEqual(trade.r_multiple, -0.25)


if __name__ == "__main__":
    unittest.main()
