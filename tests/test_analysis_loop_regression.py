from __future__ import annotations

from dataclasses import replace
import unittest

import pandas as pd

from gold_research.backtest.execution import run_backtest
from gold_research.config import ResearchConfig
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import Direction, InstrumentMetadata, PriceBasis, Signal
from gold_research.strategy.entry_point_2 import detect_entry_point_2
from gold_research.strategy.entry_point_3 import detect_entry_point_3


def _config() -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {"symbol": "XAUUSD", "provider": "test", "price_basis": "mid", "timezone": "UTC"},
            "timeframes": {"base": "15min", "medium": "1h", "large": "4h", "timezone": "UTC"},
            "strategy": {"direction": "both"},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {"enabled": True, "pullback_min_atr": 0.5, "pullback_min_bars": 2, "max_setup_bars": 6},
            "risk": {"atr_period": 2, "stop_atr": 2.0, "target_atr": 4.0, "max_hold_bars": 4},
            "costs": {
                "spread_model": "fixed",
                "spread_value": 0.4,
                "slippage_model": "fixed",
                "slippage_value": 0.1,
                "commission_per_unit": 0.2,
                "require_explicit_costs": True,
            },
            "position": {"lots": 1.0, "units_per_lot": 10.0, "leverage": 5.0},
        }
    )


def _context() -> pd.DataFrame:
    closes = [100, 100, 100, 100, 101, 102, 101, 99, 98, 99, 100]
    timestamps = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    all_up = [index < 7 for index in range(len(closes))]
    return pd.DataFrame(
        {
            "open_time": timestamps,
            "close_time": timestamps + pd.Timedelta("15min"),
            "open": closes,
            "high": [value + 0.1 for value in closes],
            "low": [value - 0.1 for value in closes],
            "close": closes,
            "signal_time": timestamps + pd.Timedelta("15min"),
            "base_trend": ["up" if value else "down" for value in all_up],
            "medium_trend": ["up" if value else "down" for value in all_up],
            "large_trend": ["up" if value else "down" for value in all_up],
            "all_up": all_up,
            "all_down": [not value for value in all_up],
            "atr": [1.0] * len(closes),
            "medium_source_close_time": timestamps,
            "large_source_close_time": timestamps - pd.Timedelta("30min"),
        }
    )


def _bars():
    rows = [
        (100, 100.5, 99.5, 100),
        (102, 107, 101, 106),
        (106, 106.5, 105.5, 106),
        (104, 105, 96, 98),
        (98, 98.5, 97.5, 98),
        (98, 98.5, 97.5, 98),
        (98, 99, 97.5, 98),
    ]
    timestamps = pd.date_range("2026-01-02", periods=len(rows), freq="15min", tz="UTC")
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


def _signal(series, index: int, side: Direction, atr: float = 1.0) -> Signal:
    entry_time = series.bars.loc[index + 1, "open_time"] if index + 1 < len(series.bars) else None
    return Signal(
        strategy_id="fixture",
        side=side,
        signal_time=series.bars.loc[index, "close_time"],
        entry_time=entry_time,
        breakout_level=100.0,
        atr=atr,
        reason="fixture",
        base_trend="up",
        medium_trend="up",
        large_trend="up",
    )


class AnalysisLoopRegressionTests(unittest.TestCase):
    def test_empty_context_does_not_require_unused_strategy_columns(self) -> None:
        empty_context = pd.DataFrame(columns=["high", "low"])

        self.assertEqual(detect_entry_point_2(empty_context, _config()), [])
        self.assertEqual(detect_entry_point_3(empty_context, _config()).signals, ())
        self.assertEqual(detect_entry_point_3(empty_context, _config()).setups, ())

    def test_no_signal_context_does_not_require_signal_metadata(self) -> None:
        context = _context().iloc[:5].copy()
        context[["all_up", "all_down"]] = False
        metadata_columns = [
            "signal_time",
            "base_trend",
            "medium_trend",
            "large_trend",
            "medium_source_close_time",
            "large_source_close_time",
        ]

        self.assertEqual(detect_entry_point_2(context.drop(columns=metadata_columns), _config()), [])
        entry_point_3_context = context.drop(columns=[*metadata_columns, "open_time"])
        entry_point_3 = detect_entry_point_3(entry_point_3_context, _config())
        self.assertEqual(entry_point_3.signals, ())
        self.assertEqual(entry_point_3.setups, ())

    def test_single_no_signal_bar_does_not_require_a_close(self) -> None:
        context = _context().iloc[:1].drop(columns="close")
        context[["all_up", "all_down"]] = False

        self.assertEqual(detect_entry_point_2(context, _config()), [])
        entry_point_3 = detect_entry_point_3(context, _config())
        self.assertEqual(entry_point_3.signals, ())
        self.assertEqual(entry_point_3.setups, ())

    def test_expired_setup_cancels_before_reading_nullable_trend(self) -> None:
        context = _context().iloc[:7].copy()
        context["all_up"] = context["all_up"].astype("boolean")
        context.loc[6, "all_up"] = pd.NA
        config = _config()
        config = replace(config, entry_point_3=replace(config.entry_point_3, max_setup_bars=1))

        result = detect_entry_point_3(context, config)

        self.assertEqual(len(result.setups), 1)
        self.assertEqual(result.setups[0].cancel_reason, "max_setup_bars_exceeded")

    def test_strategies_keep_signal_details_and_setup_audit_history(self) -> None:
        context = _context()

        entry_point_2 = detect_entry_point_2(context, _config())
        entry_point_3 = detect_entry_point_3(context, _config())

        self.assertEqual(
            [signal.to_record() for signal in entry_point_2],
            [
                {
                    "strategy_id": "entry_point_2",
                    "side": "long",
                    "signal_time": "2026-01-01T01:15:00+00:00",
                    "entry_time": "2026-01-01T01:15:00+00:00",
                    "breakout_level": 100.1,
                    "atr": 1.0,
                    "reason": "fresh_close_breakout_above_3_bar_high",
                    "base_trend": "up",
                    "medium_trend": "up",
                    "large_trend": "up",
                    "medium_source_close_time": "2026-01-01T01:00:00+00:00",
                    "large_source_close_time": "2026-01-01T00:30:00+00:00",
                    "setup_id": "",
                },
                {
                    "strategy_id": "entry_point_2",
                    "side": "short",
                    "signal_time": "2026-01-01T02:00:00+00:00",
                    "entry_time": "2026-01-01T02:00:00+00:00",
                    "breakout_level": 100.9,
                    "atr": 1.0,
                    "reason": "fresh_close_breakout_below_3_bar_low",
                    "base_trend": "down",
                    "medium_trend": "down",
                    "large_trend": "down",
                    "medium_source_close_time": "2026-01-01T01:45:00+00:00",
                    "large_source_close_time": "2026-01-01T01:15:00+00:00",
                    "setup_id": "",
                },
            ],
        )
        self.assertEqual(entry_point_3.signals, ())
        self.assertEqual(
            [
                {
                    "setup_id": setup.setup_id,
                    "side": setup.side.value,
                    "state": setup.state.value,
                    "start_index": setup.start_index,
                    "breakout_index": setup.breakout_index,
                    "breakout_level": setup.breakout_level,
                    "breakout_atr": setup.breakout_atr,
                    "extreme": setup.extreme,
                    "pullback_extreme": setup.pullback_extreme,
                    "pullback_bars": setup.pullback_bars,
                    "age_bars": setup.age_bars,
                    "cancel_reason": setup.cancel_reason,
                    "transition_log": setup.transition_log,
                }
                for setup in entry_point_3.setups
            ],
            [
                {
                    "setup_id": "long-1",
                    "side": "long",
                    "state": "cancelled",
                    "start_index": 4,
                    "breakout_index": 4,
                    "breakout_level": 100.1,
                    "breakout_atr": 1.0,
                    "extreme": 102.1,
                    "pullback_extreme": 101.1,
                    "pullback_bars": 1,
                    "age_bars": 3,
                    "cancel_reason": "trend_invalidated",
                    "transition_log": [
                        {
                            "index": 4,
                            "from": "initial_breakout",
                            "to": "waiting_pullback",
                            "reason": "initial_breakout_confirmed",
                        },
                        {
                            "index": 6,
                            "from": "waiting_pullback",
                            "to": "waiting_rebreakout",
                            "reason": "minimum_atr_pullback_reached",
                        },
                        {
                            "index": 7,
                            "from": "waiting_rebreakout",
                            "to": "cancelled",
                            "reason": "trend_invalidated",
                        },
                    ],
                },
                {
                    "setup_id": "short-2",
                    "side": "short",
                    "state": "cancelled",
                    "start_index": 7,
                    "breakout_index": 7,
                    "breakout_level": 100.9,
                    "breakout_atr": 1.0,
                    "extreme": 97.9,
                    "pullback_extreme": 98.9,
                    "pullback_bars": 2,
                    "age_bars": 3,
                    "cancel_reason": "data_ended_before_setup_completion",
                    "transition_log": [
                        {
                            "index": 7,
                            "from": "initial_breakout",
                            "to": "waiting_pullback",
                            "reason": "initial_breakout_confirmed",
                        },
                        {
                            "index": 9,
                            "from": "waiting_pullback",
                            "to": "waiting_rebreakout",
                            "reason": "minimum_atr_pullback_reached",
                        },
                        {
                            "index": 11,
                            "from": "waiting_rebreakout",
                            "to": "cancelled",
                            "reason": "data_ended_before_setup_completion",
                        },
                    ],
                },
            ],
        )

    def test_backtest_keeps_trade_and_unfilled_event_order(self) -> None:
        series = _bars()
        signals = [
            _signal(series, 0, Direction.LONG, atr=0.0),
            _signal(series, 0, Direction.LONG),
            _signal(series, 1, Direction.SHORT),
            _signal(series, 3, Direction.SHORT),
            _signal(series, 6, Direction.LONG),
        ]

        result = run_backtest(series, signals, _config())

        self.assertEqual(
            [
                (
                    trade.side.value,
                    trade.entry_price,
                    trade.exit_price,
                    trade.exit_reason,
                    trade.net_pnl,
                    trade.mfe,
                    trade.mae,
                    trade.hold_bars,
                )
                for trade in result.trades
            ],
            [
                ("long", 102.3, 106.2, "target", 35.0, 4.5, 1.5, 0),
                ("short", 105.7, 101.8, "target", 35.0, 9.5, 1.0, 1),
                ("short", 97.7, 98.3, "data_end", -10.0, 0.0, 1.5, 2),
            ],
        )
        self.assertEqual(
            result.unfilled_signals,
            (
                {"signal_time": "2026-01-02T00:15:00+00:00", "reason": "invalid_atr"},
                {"signal_time": "2026-01-02T01:45:00+00:00", "reason": "no_next_bar"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
