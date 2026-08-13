from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from gold_research.cli import build_parser, main
from gold_research.config import load_config
from gold_research.data.loader import DataSourceError
from gold_research.data.normalize import normalize_ohlc_frame
from gold_research.domain import InstrumentMetadata, PriceBasis


def _oanda_series():
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="1min", tz="UTC"),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
        }
    )
    return normalize_ohlc_frame(
        frame,
        InstrumentMetadata(
            provider="oanda",
            symbol="XAU_USD",
            price_basis=PriceBasis.MID,
            source_timezone="UTC",
            venue="OANDA spot/CFD",
            contract_unit="1 troy ounce",
            tick_size=0.01,
            point_value=1.0,
        ),
        "1min",
    )


class CliSourceTests(unittest.TestCase):
    def test_dashboard_end_defaults_to_current_utc_time(self) -> None:
        args = build_parser().parse_args(
            [
                "dashboard",
                "--config",
                "configs/xauusd_baseline.toml",
                "--start",
                "2026-01-01T00:00:00Z",
            ]
        )

        end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)

        self.assertEqual(end.tzinfo, timezone.utc)
        self.assertEqual(end.date(), now.date())
        self.assertLessEqual(end, now)
        self.assertGreater(end, now - timedelta(minutes=2))

    def test_oanda_run_passes_environment_token_and_xauusd_metadata(self) -> None:
        series = _oanda_series()
        captured = {}

        def fake_run_research(received_series, received_config, strategy_id, **kwargs):
            captured["series"] = received_series
            captured["config"] = received_config
            captured["strategy_id"] = strategy_id
            return SimpleNamespace(
                manifest=SimpleNamespace(run_id="test-run"),
                metrics={},
                warnings=(),
            )

        output = io.StringIO()
        with patch.dict(os.environ, {"OANDA_API_TOKEN": "test-token"}, clear=False):
            with patch(
                "gold_research.cli.load_oanda_candles",
                return_value=(series, "digest", "data/cache/oanda/test.json"),
            ) as loader:
                with patch("gold_research.cli.run_research", side_effect=fake_run_research):
                    with patch("gold_research.cli.write_run_artifacts", return_value="runs/test-run"):
                        with redirect_stdout(output):
                            exit_code = main(
                                [
                                    "run",
                                    "--config",
                                    "configs/xauusd_baseline.toml",
                                    "--start",
                                    "2026-01-01T00:00:00Z",
                                    "--end",
                                    "2026-01-02T00:00:00Z",
                                    "--strategy",
                                    "entry_point_2",
                                ]
                            )

        self.assertEqual(exit_code, 0)
        self.assertIs(captured["series"], series)
        self.assertEqual(captured["strategy_id"], "entry_point_2")
        self.assertEqual(loader.call_args.args[0], "XAU_USD")
        self.assertEqual(loader.call_args.args[1], "1min")
        self.assertEqual(loader.call_args.kwargs["token"], "test-token")
        metadata = loader.call_args.args[2]
        self.assertEqual(metadata.provider, "oanda")
        self.assertEqual(metadata.symbol, "XAU_USD")
        self.assertEqual(metadata.point_value, 1.0)
        self.assertEqual(metadata.tick_size, 0.01)
        self.assertIn('"provider": "oanda"', output.getvalue())

    def test_oanda_run_allows_a_cache_hit_without_token(self) -> None:
        series = _oanda_series()
        output = io.StringIO()
        cached_run = SimpleNamespace(
            manifest=SimpleNamespace(run_id="cached-run"),
            metrics={},
            warnings=(),
        )
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "gold_research.cli.load_oanda_candles",
                return_value=(series, "digest", "data/cache/oanda/cached.json"),
            ) as loader:
                with patch("gold_research.cli.run_research", return_value=cached_run):
                    with patch("gold_research.cli.write_run_artifacts", return_value="runs/cached-run"):
                        with redirect_stdout(output):
                            exit_code = main(
                                [
                                    "run",
                                    "--config",
                                    "configs/xauusd_baseline.toml",
                                    "--start",
                                    "2026-01-01T00:00:00Z",
                                    "--end",
                                    "2026-01-02T00:00:00Z",
                                    "--strategy",
                                    "entry_point_2",
                                ]
                            )

        self.assertEqual(exit_code, 0)
        self.assertIsNone(loader.call_args.kwargs["token"])

    def test_run_requires_oanda_token_on_a_cache_miss(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True):
            with patch("gold_research.cli.load_oanda_candles", side_effect=DataSourceError("OANDA_API_TOKEN is not set")):
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "run",
                            "--config",
                            "configs/xauusd_baseline.toml",
                            "--start",
                            "2026-01-01T00:00:00Z",
                            "--end",
                            "2026-01-02T00:00:00Z",
                            "--strategy",
                            "entry_point_2",
                        ]
                    )

        self.assertEqual(exit_code, 2)
        self.assertIn("OANDA_API_TOKEN", output.getvalue())

    def test_legacy_data_source_flags_are_rejected(self) -> None:
        for legacy_flag, value in (
            ("--input", "bars.csv"),
            ("--yahoo-symbol", "GC=F"),
            ("--oanda-instrument", "XAU_USD"),
        ):
            with self.subTest(legacy_flag=legacy_flag):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(
                        [
                            "run",
                            "--config",
                            "configs/xauusd_baseline.toml",
                            legacy_flag,
                            value,
                            "--start",
                            "2026-01-01T00:00:00Z",
                            "--end",
                            "2026-01-02T00:00:00Z",
                            "--strategy",
                            "entry_point_2",
                        ]
                    )

    def test_entry_point_3_strategy_is_not_available(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "run",
                    "--config",
                    "configs/xauusd_baseline.toml",
                    "--start",
                    "2026-01-01T00:00:00Z",
                    "--end",
                    "2026-01-02T00:00:00Z",
                    "--strategy",
                    "entry_point_3",
                ]
            )

    def test_validate_config_rejects_non_oanda_contract(self) -> None:
        config = load_config("configs/xauusd_baseline.toml")
        non_oanda = replace(config, instrument=replace(config.instrument, provider="other-provider"))
        output = io.StringIO()
        with patch("gold_research.cli.load_config", return_value=non_oanda):
            with redirect_stdout(output):
                exit_code = main(["validate-config", "--config", "ignored.toml"])

        self.assertEqual(exit_code, 2)
        self.assertIn("OANDA", output.getvalue())


if __name__ == "__main__":
    unittest.main()
