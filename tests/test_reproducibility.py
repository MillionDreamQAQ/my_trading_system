from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from gold_research.config import ResearchConfig
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import Direction, InstrumentMetadata, PriceBasis, Signal
from gold_research.reporting.artifacts import write_run_artifacts
from gold_research.research import evaluate_signal_quality, run_research


def _config() -> ResearchConfig:
    return ResearchConfig.from_mapping(
        {
            "instrument": {
                "symbol": "XAU_USD",
                "provider": "oanda",
                "price_basis": "mid",
                "timezone": "UTC",
                "venue": "OANDA spot/CFD",
                "contract_unit": "1 troy ounce",
                "quote_currency": "USD",
                "tick_size": 0.01,
                "point_value": 1.0,
            },
            "timeframes": {"base": "15min", "medium": "1h", "large": "4h", "timezone": "UTC"},
            "strategy": {"direction": "both", "version": "test-v1"},
            "trend": {"ema_fast": 2, "ema_slow": 3, "slope_lookback": 1},
            "entry_point_2": {"enabled": True, "breakout_lookback": 3},
            "entry_point_3": {"enabled": True, "pullback_min_atr": 0.5, "pullback_min_bars": 2, "max_setup_bars": 30},
            "risk": {"atr_period": 2, "stop_atr": 2.0, "target_atr": 4.0, "max_hold_bars": 20},
            "costs": {
                "spread_model": "fixed", "spread_value": 0.0,
                "slippage_model": "fixed", "slippage_value": 0.0,
                "commission_per_unit": 0.0, "require_explicit_costs": True,
            },
        }
    )


def _bars():
    values = [100.0 + 0.2 * index for index in range(64)]
    values.extend([112.6] * 6)
    values.extend([115.0 + 0.1 * index for index in range(30)])
    timestamps = pd.date_range("2026-01-01", periods=len(values), freq="15min", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": values,
            "high": [value + 0.1 for value in values],
            "low": [value - 0.1 for value in values],
            "close": values,
        }
    )
    return normalize_ohlc_frame(
        frame,
        InstrumentMetadata(
            provider="oanda",
            symbol="XAU_USD",
            price_basis=PriceBasis.MID,
            venue="OANDA spot/CFD",
            contract_unit="1 troy ounce",
            tick_size=0.01,
            point_value=1.0,
        ),
        "15min",
    )


def _file_hashes(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_file():
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


class ReproducibilityTests(unittest.TestCase):
    def test_same_input_produces_same_artifacts_and_separate_quality_windows(self) -> None:
        base = _bars()
        config = _config()
        first = run_research(base, config, "entry_point_2", data_fingerprint="fixture-hash", code_root="src/gold_research")
        second = run_research(base, config, "entry_point_2", data_fingerprint="fixture-hash", code_root="src/gold_research")

        self.assertGreaterEqual(len(first.signals), 1)
        self.assertEqual(first.manifest.to_dict(), second.manifest.to_dict())
        self.assertEqual(first.metrics, second.metrics)
        self.assertEqual(first.signal_quality, second.signal_quality)

        with tempfile.TemporaryDirectory() as temporary:
            first_dir = write_run_artifacts(first, Path(temporary) / "first")
            second_dir = write_run_artifacts(second, Path(temporary) / "second")
            self.assertEqual(_file_hashes(first_dir), _file_hashes(second_dir))
            self.assertTrue((first_dir / "manifest.json").is_file())
            self.assertTrue((first_dir / "signals.csv").is_file())
            self.assertTrue((first_dir / "signal_quality.csv").is_file())
            self.assertTrue((first_dir / "trades.csv").is_file())
            self.assertTrue((first_dir / "metrics.json").is_file())
            self.assertTrue((first_dir / "warnings.json").is_file())
            self.assertTrue((first_dir / "report.md").is_file())

    def test_oanda_source_metadata_marks_run_research_usable(self) -> None:
        run = run_research(_bars(), _config(), "entry_point_2", data_fingerprint="fixture-hash", code_root="src/gold_research")

        self.assertTrue(run.manifest.research_usable)
        self.assertFalse(any(issue.code == "UNVERIFIED_SOURCE_METADATA" for issue in run.warnings))

    def test_entry_point_3_research_is_temporarily_disabled(self) -> None:
        with self.assertRaisesRegex(ValueError, "entry_point_3.*disabled"):
            run_research(_bars(), _config(), "entry_point_3", data_fingerprint="fixture-hash", code_root="src/gold_research")

    def test_signal_quality_mfe_and_mae_use_non_negative_directional_basis(self) -> None:
        values = [100.0] * 20
        values[11:16] = [99.0, 98.0, 99.0, 97.0, 98.0]
        timestamps = pd.date_range("2026-01-01", periods=len(values), freq="15min", tz="UTC")
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": values,
                "high": [value + 0.1 for value in values],
                "low": [value - 0.1 for value in values],
                "close": values,
            }
        )
        series = normalize_ohlc_frame(
            frame,
            InstrumentMetadata(
                provider="oanda",
                symbol="XAU_USD",
                price_basis=PriceBasis.MID,
                venue="OANDA spot/CFD",
                contract_unit="1 troy ounce",
                tick_size=0.01,
                point_value=1.0,
            ),
            "15min",
        )
        signal = Signal(
            strategy_id="entry_point_2",
            side=Direction.LONG,
            signal_time=series.bars.loc[10, "close_time"],
            entry_time=series.bars.loc[11, "open_time"],
            breakout_level=100.0,
            atr=1.0,
            reason="test",
            base_trend="up",
            medium_trend="up",
            large_trend="up",
        )

        records = evaluate_signal_quality(series, [signal], windows=(5,))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["mfe"], 0.0)
        self.assertGreater(records[0]["mae"], 0.0)

    def test_signal_quality_preserves_window_order_and_skip_rules(self) -> None:
        timestamps = pd.date_range("2026-01-01", periods=6, freq="15min", tz="UTC")
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100.0, 102.0, 99.0, 101.0, 98.0, 100.0],
                "high": [101.0, 104.0, 100.0, 103.0, 99.0, 101.0],
                "low": [99.0, 100.0, 95.0, 98.0, 94.0, 97.0],
                "close": [100.0, 102.0, 99.0, 101.0, 98.0, 100.0],
            }
        )
        series = normalize_ohlc_frame(
            frame,
            InstrumentMetadata(
                provider="oanda",
                symbol="XAU_USD",
                price_basis=PriceBasis.MID,
            ),
            "15min",
        )
        long_signal = Signal(
            strategy_id="entry_point_2",
            side=Direction.LONG,
            signal_time=series.bars.loc[0, "close_time"],
            entry_time=series.bars.loc[1, "open_time"],
            breakout_level=100.0,
            atr=1.0,
            reason="test",
            base_trend="up",
            medium_trend="up",
            large_trend="up",
        )
        short_signal = Signal(
            strategy_id="entry_point_2",
            side=Direction.SHORT,
            signal_time=series.bars.loc[1, "close_time"],
            entry_time=series.bars.loc[2, "open_time"],
            breakout_level=102.0,
            atr=1.0,
            reason="test",
            base_trend="down",
            medium_trend="down",
            large_trend="down",
        )
        missing_signal = replace(long_signal, signal_time=pd.Timestamp("2026-01-03T00:00:00Z"))

        records = evaluate_signal_quality(
            series,
            [long_signal, short_signal, missing_signal],
            windows=(2, 2, 0, -1, 10, 1),
        )

        self.assertEqual(
            [(record["side"], record["window_bars"]) for record in records],
            [("long", 2), ("long", 2), ("long", 1), ("short", 2), ("short", 2), ("short", 1)],
        )
        self.assertEqual(records[0]["signal_close"], 100.0)
        self.assertAlmostEqual(records[0]["forward_return"], -0.01)
        self.assertAlmostEqual(records[0]["mfe"], 0.04)
        self.assertAlmostEqual(records[0]["mae"], 0.05)
        self.assertAlmostEqual(records[3]["forward_return"], 1.0 - 101.0 / 102.0)
        self.assertAlmostEqual(records[3]["mfe"], 1.0 - 95.0 / 102.0)
        self.assertAlmostEqual(records[3]["mae"], 103.0 / 102.0 - 1.0)

    def test_signal_quality_keeps_empty_and_unmatched_inputs_lazy(self) -> None:
        series = _bars()
        signal = Signal(
            strategy_id="entry_point_2",
            side=Direction.LONG,
            signal_time=pd.Timestamp("2026-01-03T00:00:00Z"),
            entry_time=None,
            breakout_level=100.0,
            atr=1.0,
            reason="test",
            base_trend="up",
            medium_trend="up",
            large_trend="up",
        )
        series.bars = series.bars[["open_time", "close_time"]]

        self.assertEqual(evaluate_signal_quality(series, []), ())
        self.assertEqual(evaluate_signal_quality(series, [signal]), ())

    def test_signal_quality_preserves_object_price_column_behavior(self) -> None:
        timestamps = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
        series = normalize_ohlc_frame(
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "open": [8.0, 7.0, 6.0],
                    "high": [9.0, 10.0, 8.0],
                    "low": [7.0, 6.0, 5.0],
                    "close": [8.0, 7.0, 6.0],
                }
            ),
            InstrumentMetadata(provider="oanda", symbol="XAU_USD", price_basis=PriceBasis.MID),
            "15min",
        )
        for column in ("high", "low", "close"):
            series.bars[column] = series.bars[column].astype(str)
        signal = Signal(
            strategy_id="entry_point_2",
            side=Direction.LONG,
            signal_time=series.bars.loc[0, "close_time"],
            entry_time=series.bars.loc[1, "open_time"],
            breakout_level=8.0,
            atr=1.0,
            reason="test",
            base_trend="up",
            medium_trend="up",
            large_trend="up",
        )

        records = evaluate_signal_quality(series, [signal], windows=(2,))

        self.assertEqual(records[0]["mfe"], 0.0)
        self.assertAlmostEqual(records[0]["mae"], 1.0 - 5.0 / 8.0)

    def test_research_uses_oanda_point_value_for_backtest(self) -> None:
        frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC"),
                "open": [100.0, 102.0, 102.5],
                "high": [100.5, 107.0, 103.0],
                "low": [99.5, 101.5, 102.0],
                "close": [100.0, 106.0, 102.5],
            }
        )
        base = normalize_ohlc_frame(
            frame,
            InstrumentMetadata(
                provider="oanda",
                symbol="XAU_USD",
                price_basis=PriceBasis.MID,
                source_timezone="UTC",
                venue="OANDA spot/CFD",
                contract_unit="1 troy ounce",
                quote_currency="USD",
                tick_size=0.01,
                point_value=1.0,
            ),
            "15min",
        )
        signal = Signal(
            strategy_id="entry_point_2",
            side=Direction.LONG,
            signal_time=base.bars.loc[0, "close_time"],
            entry_time=base.bars.loc[1, "open_time"],
            breakout_level=100.0,
            atr=1.0,
            reason="test",
            base_trend="up",
            medium_trend="up",
            large_trend="up",
        )

        with patch("gold_research.research.detect_entry_point_2", return_value=[signal]):
            run = run_research(
                base,
                _config(),
                "entry_point_2",
                data_fingerprint="fixture-hash",
                code_root="src/gold_research",
            )

        self.assertEqual(len(run.backtest.trades), 1)
        self.assertEqual(run.backtest.trades[0].gross_pnl, 4.0)

    def test_research_rejects_config_data_contract_mismatch(self) -> None:
        invalid = replace(_config(), instrument=replace(_config().instrument, point_value=100.0))

        with self.assertRaisesRegex(ValueError, "point_value"):
            run_research(_bars(), invalid, "entry_point_2", data_fingerprint="fixture-hash", code_root="src/gold_research")

    def test_research_rejects_non_oanda_source_metadata(self) -> None:
        base = _bars()
        base.metadata = replace(base.metadata, provider="other-provider")

        with self.assertRaisesRegex(ValueError, "oanda"):
            run_research(base, _config(), "entry_point_2", data_fingerprint="fixture-hash", code_root="src/gold_research")


if __name__ == "__main__":
    unittest.main()
