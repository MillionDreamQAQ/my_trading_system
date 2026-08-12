from __future__ import annotations

import unittest

import pandas as pd

from gold_research.backtest.costs import CostModel
from gold_research.backtest.execution import run_backtest
from gold_research.config import ResearchConfig
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import Direction, InstrumentMetadata, PriceBasis, Signal


def _config(spread: float = 0.0, slippage: float = 0.0, max_hold_bars: int = 80) -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {"symbol": "XAUUSD", "provider": "test", "price_basis": "mid", "timezone": "UTC", "point_value": 1.0},
            "timeframes": {"base": "15min", "medium": "1h", "large": "4h", "timezone": "UTC"},
            "strategy": {"direction": "both"},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {"enabled": True, "pullback_min_atr": 0.5, "pullback_min_bars": 2, "max_setup_bars": 30},
            "risk": {"atr_period": 2, "stop_atr": 2.0, "target_atr": 4.0, "max_hold_bars": max_hold_bars},
            "costs": {
                "spread_model": "fixed", "spread_value": spread,
                "slippage_model": "fixed", "slippage_value": slippage,
                "commission_per_unit": 0.0, "require_explicit_costs": True,
            },
        }
    )


def _bars(rows: list[tuple[float, float, float, float]]):
    timestamps = pd.date_range("2026-01-01", periods=len(rows), freq="15min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
        }
    )
    return normalize_ohlc_frame(
        frame,
        InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
        "15min",
    )


def _signal(index: int, atr: float = 1.0) -> Signal:
    signal_time = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15 * index)
    entry_time = signal_time + pd.Timedelta(minutes=15)
    return Signal(
        strategy_id="entry_point_2",
        side=Direction.LONG,
        signal_time=signal_time,
        entry_time=entry_time,
        breakout_level=100.0,
        atr=atr,
        reason="test",
        base_trend="up",
        medium_trend="up",
        large_trend="up",
    )


class BacktestExecutionTests(unittest.TestCase):
    def test_signal_fills_at_next_bar_open_and_target_exits(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (102, 107, 101.5, 106), (102.5, 103, 102, 102.5)])

        result = run_backtest(bars, [_signal(0)], _config())

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.entry_time, bars.bars.loc[1, "open_time"])
        self.assertEqual(trade.exit_reason, "target")
        self.assertEqual(trade.entry_price, 102.0)
        self.assertEqual(trade.exit_price, 106.0)
        self.assertEqual(trade.net_pnl, 4.0)

    def test_nonzero_spread_and_slippage_reduce_net_result(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (100, 105, 99, 101), (101, 101, 100, 100.5)])

        zero = run_backtest(bars, [_signal(0)], _config())
        costly = run_backtest(bars, [_signal(0)], _config(spread=0.4, slippage=0.1))

        self.assertEqual(len(zero.trades), len(costly.trades))
        self.assertLess(costly.trades[0].net_pnl, zero.trades[0].net_pnl)
        self.assertGreater(costly.trades[0].spread_cost, 0)
        self.assertGreater(costly.trades[0].slippage_cost, 0)

    def test_stop_has_priority_when_both_levels_are_touched(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (100, 105, 95, 100), (100, 100, 100, 100)])

        result = run_backtest(bars, [_signal(0)], _config())

        self.assertEqual(result.trades[0].exit_reason, "stop")

    def test_open_gap_through_stop_exits_at_open(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (100, 101, 99, 100), (90, 91, 89, 90), (90, 90, 90, 90)])

        result = run_backtest(bars, [_signal(0)], _config())

        self.assertEqual(result.trades[0].exit_reason, "stop")
        self.assertEqual(result.trades[0].exit_price, 90.0)

    def test_mid_basis_stop_uses_the_executable_bid_low(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (100, 100.1, 99.9, 100), (100, 100.1, 98.3, 99)])

        result = run_backtest(bars, [_signal(0)], _config(spread=0.4))

        self.assertEqual(result.trades[0].exit_reason, "stop")
        self.assertEqual(result.trades[0].exit_price, 98.2)
        self.assertAlmostEqual(result.trades[0].net_pnl, -2.0)

    def test_timeout_exit_records_the_bar_close_time(self) -> None:
        bars = _bars(
            [
                (100, 100.5, 99.5, 100),
                (100, 100.5, 99.5, 100),
                (100, 100.5, 99.5, 100),
            ]
        )

        result = run_backtest(bars, [_signal(0)], _config(max_hold_bars=1))

        self.assertEqual(result.trades[0].exit_reason, "timeout")
        self.assertEqual(result.trades[0].exit_time, bars.bars.loc[2, "close_time"])

    def test_data_end_exit_records_the_final_bar_close_time(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100)])

        result = run_backtest(bars, [_signal(0)], _config())

        self.assertEqual(result.trades[0].exit_reason, "data_end")
        self.assertEqual(result.trades[0].exit_time, bars.bars.loc[1, "close_time"])

    def test_pending_signal_without_next_bar_is_not_a_trade(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100)])

        signal = _signal(0)
        signal = Signal(
            strategy_id=signal.strategy_id,
            side=signal.side,
            signal_time=signal.signal_time,
            entry_time=None,
            breakout_level=signal.breakout_level,
            atr=signal.atr,
            reason=signal.reason,
            base_trend=signal.base_trend,
            medium_trend=signal.medium_trend,
            large_trend=signal.large_trend,
        )

        result = run_backtest(bars, [signal], _config())

        self.assertEqual(result.trades, ())
        self.assertEqual(result.unfilled_signals[0]["reason"], "no_next_bar")

    def test_invalid_first_candidate_does_not_block_valid_same_open_time_signal(self) -> None:
        bars = _bars([(100, 100.5, 99.5, 100), (102, 107, 101.5, 106), (102.5, 103, 102, 102.5)])
        invalid = _signal(0, atr=0.0)
        valid = _signal(0, atr=1.0)

        result = run_backtest(bars, [invalid, valid], _config())

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].entry_price, 102.0)
        self.assertEqual(result.unfilled_signals[0]["reason"], "invalid_atr")


class CostModelTests(unittest.TestCase):
    def test_long_round_trip_uses_ask_then_bid(self) -> None:
        model = CostModel(spread=0.4, slippage=0.0, commission_per_unit=0.0, source_basis=PriceBasis.MID)

        self.assertEqual(model.execution_price(100.0, Direction.LONG, "entry"), 100.2)
        self.assertEqual(model.execution_price(100.0, Direction.LONG, "exit"), 99.8)

    def test_quote_and_reference_conversions_preserve_each_price_basis(self) -> None:
        for basis, expected_entry_quote, expected_exit_quote in (
            (PriceBasis.MID, 100.2, 99.8),
            (PriceBasis.BID, 100.4, 100.0),
            (PriceBasis.ASK, 100.0, 99.6),
        ):
            with self.subTest(basis=basis):
                model = CostModel(spread=0.4, slippage=0.0, commission_per_unit=0.0, source_basis=basis)
                entry_quote = model.quote_price(100.0, Direction.LONG, "entry")
                exit_quote = model.quote_price(100.0, Direction.LONG, "exit")

                self.assertEqual(entry_quote, expected_entry_quote)
                self.assertEqual(exit_quote, expected_exit_quote)
                self.assertEqual(model.reference_price(entry_quote, Direction.LONG, "entry"), 100.0)
                self.assertEqual(model.reference_price(exit_quote, Direction.LONG, "exit"), 100.0)


if __name__ == "__main__":
    unittest.main()
