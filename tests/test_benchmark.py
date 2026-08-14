from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.benchmark_two_year_backtest import (
    BENCHMARK_SCENARIOS,
    BASELINE_SOURCE_COMMIT,
    MIN_BENCHMARK_RUNS,
    _compare_baseline,
    main,
    run_benchmark,
)


class BenchmarkContractTests(unittest.TestCase):
    def test_benchmark_requires_at_least_three_runs(self) -> None:
        with patch("sys.argv", ["benchmark_two_year_backtest.py", "--runs", "1"]):
            with self.assertRaises(SystemExit):
                main()

        with self.assertRaises(ValueError):
            run_benchmark(Path("configs/xauusd_baseline.toml"), Path("data/cache/oanda.sqlite3"), 1)

        self.assertEqual(MIN_BENCHMARK_RUNS, 3)

    def test_checked_in_baseline_covers_required_variants(self) -> None:
        baseline_path = Path("benchmarks/baseline_two_year_backtest.json")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        scenarios = baseline["scenarios"]

        self.assertEqual({item["direction"] for item in scenarios.values()}, {"long", "short", "both"})
        self.assertEqual({item["price_basis"] for item in scenarios.values()}, {"mid", "bid", "ask"})
        self.assertTrue(any(item["spread_value"] > 0 for item in scenarios.values()))
        self.assertTrue(any(item["max_positions"] > 1 for item in scenarios.values()))
        self.assertEqual(set(scenarios), {scenario.name for scenario in BENCHMARK_SCENARIOS})
        self.assertEqual(baseline["source_commit"], BASELINE_SOURCE_COMMIT)
        self.assertEqual(scenarios["both_mid"]["price_source"], "cached_mid")
        self.assertEqual(scenarios["both_bid"]["price_source"], "synthetic_from_mid")
        self.assertEqual(scenarios["both_ask"]["price_source"], "synthetic_from_mid")

    def test_baseline_comparison_rejects_a_changed_summary(self) -> None:
        baseline_path = Path("benchmarks/baseline_two_year_backtest.json")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        scenarios = {
            name: {
                "scenario": {
                    key: value
                    for key, value in item.items()
                    if key != "summary"
                },
                "summary": item["summary"].copy(),
            }
            for name, item in baseline["scenarios"].items()
        }
        scenarios["both_mid"]["summary"]["signals"] = {
            **scenarios["both_mid"]["summary"]["signals"],
            "count": scenarios["both_mid"]["summary"]["signals"]["count"] + 1,
        }

        comparison = _compare_baseline(
            baseline=baseline,
            data_fingerprint=baseline["data_fingerprint"],
            scenarios=scenarios,
            baseline_path=baseline_path,
        )

        self.assertFalse(comparison["matched"])
        self.assertTrue(any(item["kind"] == "scenario_summary" for item in comparison["differences"]))


if __name__ == "__main__":
    unittest.main()
