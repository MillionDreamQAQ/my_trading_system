from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

from gold_research.config import ResearchConfig
from gold_research.dashboard import (
    DASHBOARD_WARMUP,
    DashboardDataStore,
    build_backtest_analysis,
    build_dashboard_payload,
    dashboard_error_message,
)
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import InstrumentMetadata, PriceBasis


def _config() -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {"symbol": "XAUUSD", "provider": "test", "price_basis": "mid", "timezone": "UTC"},
            "timeframes": {"base": "1min", "medium": "5min", "large": "30min", "timezone": "UTC"},
            "strategy": {"direction": "both"},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {"enabled": True, "pullback_min_atr": 0.5, "pullback_min_bars": 2, "max_setup_bars": 10},
            "risk": {"atr_period": 2, "stop_atr": 2.0, "target_atr": 4.0, "max_hold_bars": 5},
            "costs": {"spread_model": "fixed", "spread_value": 0.0, "slippage_model": "fixed", "slippage_value": 0.0, "commission_per_unit": 0.0, "require_explicit_costs": True},
            "data_quality": {"missing_bar_policy": "block", "max_gap_bars": 0},
        }
    )


class DashboardTests(unittest.TestCase):
    def _series(self, start: pd.Timestamp, end: pd.Timestamp):
        timestamps = pd.date_range(start, end, freq="1min", inclusive="left", tz="UTC")
        values = [100 + index * 0.01 for index in range(len(timestamps))]
        return normalize_ohlc_frame(
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "open": values,
                    "high": [value + 0.2 for value in values],
                    "low": [value - 0.2 for value in values],
                    "close": values,
                }
            ),
            InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
            "1min",
        )

    def test_payload_uses_warmup_bars_but_only_displays_target_window(self) -> None:
        timestamps = pd.date_range("2026-01-01", periods=181, freq="1min", tz="UTC")
        values = [100 + index * 0.1 for index in range(len(timestamps))]
        base = normalize_ohlc_frame(
            pd.DataFrame({"timestamp": timestamps, "open": values, "high": [value + 0.2 for value in values], "low": [value - 0.2 for value in values], "close": values}),
            InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
            "1min",
        )

        payload = build_dashboard_payload(
            base,
            _config(),
            display_start=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
            display_end=datetime(2026, 1, 1, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(len(payload["series"]["1min"]), 60)
        self.assertEqual(len(payload["series"]["5min"]), 12)
        self.assertEqual(len(payload["series"]["30min"]), 2)
        self.assertEqual(payload["metadata"]["warmup_start"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(payload["metadata"]["available_start"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(payload["metadata"]["available_end"], "2026-01-01T03:01:00+00:00")
        self.assertEqual(payload["metadata"]["display_start"], "2026-01-01T02:00:00+00:00")
        self.assertEqual(payload["metadata"]["trend"]["ema_slow"], 3)
        self.assertIn("ema_fast", payload["series"]["1min"][0])
        self.assertIn("trend", payload["series"]["30min"][0])
        for strategy in payload["strategies"].values():
            self.assertIn("1min", strategy["signals"])
            self.assertIn("30min", strategy["trades"])

    def test_payload_rejects_a_window_outside_loaded_data(self) -> None:
        timestamps = pd.date_range("2026-01-01", periods=10, freq="1min", tz="UTC")
        base = normalize_ohlc_frame(
            pd.DataFrame({"timestamp": timestamps, "open": range(10), "high": range(10), "low": range(10), "close": range(10)}),
            InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
            "1min",
        )

        with self.assertRaisesRegex(ValueError, "loaded data window"):
            build_dashboard_payload(
                base,
                _config(),
                display_start=datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc),
                display_end=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            )

    def test_payload_allows_an_end_inside_a_normal_weekend_closure(self) -> None:
        timestamps = pd.date_range("2026-08-07T20:00:00Z", periods=60, freq="1min", tz="UTC")
        base = normalize_ohlc_frame(
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "open": range(100, 160),
                    "high": range(100, 160),
                    "low": range(100, 160),
                    "close": range(100, 160),
                }
            ),
            InstrumentMetadata(provider="test", symbol="XAUUSD", price_basis=PriceBasis.MID),
            "1min",
        )
        config = ResearchConfig.from_mapping(
            {
                **_config().to_dict(),
                "data_quality": {
                    "missing_bar_policy": "block",
                    "max_gap_bars": 0,
                    "closed_weekdays": [5],
                    "market_calendar": "oanda_xau_usd",
                },
            }
        )

        payload = build_dashboard_payload(
            base,
            config,
            display_start=datetime(2026, 8, 7, 20, tzinfo=timezone.utc),
            display_end=datetime(2026, 8, 8, 21, tzinfo=timezone.utc),
        )

        self.assertEqual(len(payload["series"]["1min"]), 60)

    def test_data_store_loads_outside_window_with_indicator_warmup_and_caches_it(self) -> None:
        initial_start = pd.Timestamp("2026-01-08T00:00:00Z")
        initial_end = initial_start + pd.Timedelta(hours=3)
        requested_start = pd.Timestamp("2026-01-10T02:00:00Z")
        requested_end = requested_start + pd.Timedelta(hours=1)
        calls: list[tuple[pd.Timestamp, pd.Timestamp]] = []

        def load_window(start: datetime, end: datetime):
            start_timestamp = pd.Timestamp(start)
            end_timestamp = pd.Timestamp(end)
            calls.append((start_timestamp, end_timestamp))
            return self._series(start_timestamp, end_timestamp)

        store = DashboardDataStore(
            self._series(initial_start, initial_end),
            _config(),
            load_window,
            initial_start=initial_start.to_pydatetime(),
            initial_end=initial_end.to_pydatetime(),
        )

        payload = store.payload_for(requested_start.to_pydatetime(), requested_end.to_pydatetime())

        self.assertEqual(len(payload["series"]["1min"]), 60)
        self.assertEqual(payload["metadata"]["display_start"], requested_start.isoformat())
        self.assertEqual(calls[0][0], requested_start - DASHBOARD_WARMUP)
        first_call_count = len(calls)

        store.payload_for(requested_start.to_pydatetime(), requested_end.to_pydatetime())

        self.assertEqual(len(calls), first_call_count)

    def test_missing_token_error_is_actionable_in_chinese(self) -> None:
        message = dashboard_error_message(RuntimeError("OANDA_API_TOKEN is not set"))

        self.assertIn("OANDA_API_TOKEN", message)
        self.assertIn("重启", message)

    def test_backtest_analysis_reports_equity_and_trade_attribution(self) -> None:
        trades = [
            type(
                "Trade",
                (),
                {
                    "net_pnl": 4.0,
                    "exit_time": pd.Timestamp("2026-01-01T00:10:00Z"),
                    "hold_bars": 3,
                    "side": type("Side", (), {"value": "long"})(),
                    "exit_reason": "target",
                },
            )(),
            type(
                "Trade",
                (),
                {
                    "net_pnl": -2.0,
                    "exit_time": pd.Timestamp("2026-01-01T00:20:00Z"),
                    "hold_bars": 5,
                    "side": type("Side", (), {"value": "short"})(),
                    "exit_reason": "stop",
                },
            )(),
        ]

        analysis = build_backtest_analysis(trades, signal_count=4, unfilled_signal_count=2)

        self.assertEqual(analysis["trade_count"], 2)
        self.assertEqual(analysis["fill_rate"], 0.5)
        self.assertEqual(analysis["profit_factor"], 2.0)
        self.assertEqual(analysis["max_consecutive_wins"], 1)
        self.assertEqual(analysis["max_consecutive_losses"], 1)
        self.assertEqual([point["value"] for point in analysis["equity_curve"]], [4.0, 2.0])
        self.assertEqual(analysis["by_side"]["long"]["net_pnl"], 4.0)
        self.assertEqual(analysis["by_exit_reason"]["stop"]["net_pnl"], -2.0)


if __name__ == "__main__":
    unittest.main()
