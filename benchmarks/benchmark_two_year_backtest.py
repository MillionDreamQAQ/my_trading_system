"""Measure the fixed two-year cached backtest without making network requests."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Callable
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from gold_research import research as research_module  # noqa: E402
from gold_research.config import load_config  # noqa: E402
from gold_research.data.loader import load_oanda_candles  # noqa: E402
from gold_research.domain import BarSeries, Direction, InstrumentMetadata, PriceBasis  # noqa: E402
from gold_research.reporting.artifacts import write_run_artifacts  # noqa: E402


START = datetime(2024, 7, 25, tzinfo=timezone.utc)
END = datetime(2026, 7, 25, tzinfo=timezone.utc)
MIN_BENCHMARK_RUNS = 3
DEFAULT_BASELINE_PATH = REPOSITORY_ROOT / "benchmarks" / "baseline_two_year_backtest.json"
BASELINE_SOURCE_COMMIT = "873e7d26d90ccdba3b7ecbbadbd2cdd66583af0f"
SYNTHETIC_HALF_SPREAD = 0.2


@dataclass(frozen=True)
class BenchmarkScenario:
    name: str
    direction: Direction
    price_basis: PriceBasis
    spread_value: float = 0.0
    slippage_value: float = 0.0
    commission_per_unit: float = 0.0
    max_positions: int = 1
    price_source: str = "cached_mid"

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction.value,
            "price_basis": self.price_basis.value,
            "spread_value": self.spread_value,
            "slippage_value": self.slippage_value,
            "commission_per_unit": self.commission_per_unit,
            "max_positions": self.max_positions,
            "price_source": self.price_source,
        }


BENCHMARK_SCENARIOS = (
    BenchmarkScenario("both_mid", Direction.BOTH, PriceBasis.MID),
    BenchmarkScenario("long_mid", Direction.LONG, PriceBasis.MID),
    BenchmarkScenario("short_mid", Direction.SHORT, PriceBasis.MID),
    BenchmarkScenario("both_bid", Direction.BOTH, PriceBasis.BID, price_source="synthetic_from_mid"),
    BenchmarkScenario("both_ask", Direction.BOTH, PriceBasis.ASK, price_source="synthetic_from_mid"),
    BenchmarkScenario(
        "both_mid_costs",
        Direction.BOTH,
        PriceBasis.MID,
        spread_value=0.4,
        slippage_value=0.1,
        commission_per_unit=0.2,
    ),
    BenchmarkScenario("both_mid_parallel", Direction.BOTH, PriceBasis.MID, max_positions=2),
)


def _rss_bytes() -> int:
    if os.name == "nt":
        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()
        get_process_memory_info = ctypes.WinDLL("Psapi.dll").GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        if get_process_memory_info(
            process,
            ctypes.byref(counters),
            counters.cb,
        ):
            return int(counters.WorkingSetSize)
        return 0

    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _metadata(config) -> InstrumentMetadata:
    instrument = config.instrument
    return InstrumentMetadata(
        provider=instrument.provider,
        symbol=instrument.symbol,
        price_basis=instrument.price_basis,
        source_timezone=instrument.timezone,
        venue=instrument.venue,
        contract_unit=instrument.contract_unit,
        quote_currency=instrument.quote_currency,
        tick_size=instrument.tick_size,
        point_value=instrument.point_value,
    )


def _scenario_config(config, scenario: BenchmarkScenario):
    return replace(
        config,
        direction=scenario.direction,
        instrument=replace(config.instrument, price_basis=scenario.price_basis),
        costs=replace(
            config.costs,
            spread_value=scenario.spread_value,
            slippage_value=scenario.slippage_value,
            commission_per_unit=scenario.commission_per_unit,
        ),
        position=replace(config.position, max_positions=scenario.max_positions),
    )


def _scenario_base(base: BarSeries, scenario: BenchmarkScenario) -> BarSeries:
    if base.metadata.price_basis is scenario.price_basis:
        return base
    variant = base.copy()
    if scenario.price_basis is PriceBasis.BID:
        offset = -SYNTHETIC_HALF_SPREAD
    elif scenario.price_basis is PriceBasis.ASK:
        offset = SYNTHETIC_HALF_SPREAD
    else:
        offset = 0.0
    if offset:
        for column in ("open", "high", "low", "close"):
            variant.bars[column] = variant.bars[column] + offset
    variant.metadata = replace(variant.metadata, price_basis=scenario.price_basis)
    return variant


def _load_cached(config, database: Path) -> tuple[Any, str, float]:
    previous_token = os.environ.pop("OANDA_API_TOKEN", None)
    started = perf_counter()
    try:
        series, digest, _ = load_oanda_candles(
            "XAU_USD",
            config.timeframes.base,
            _metadata(config),
            start=START,
            end=END,
            token=None,
            database_path=database,
            compute_digest=True,
        )
    finally:
        if previous_token is not None:
            os.environ["OANDA_API_TOKEN"] = previous_token
    return series, digest, perf_counter() - started


def _timed(
    timings: dict[str, float],
    name: str,
    function: Callable[..., Any],
) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            timings[name] = perf_counter() - started

    return wrapped


def _run_timed_research(base, config, data_fingerprint: str):
    timings: dict[str, float] = {}
    resample_count = 0
    original_resample = research_module.resample_bars

    def timed_resample(*args: Any, **kwargs: Any) -> Any:
        nonlocal resample_count
        resample_count += 1
        name = "resample_medium" if resample_count == 1 else "resample_large"
        return _timed(timings, name, original_resample)(*args, **kwargs)

    with (
        patch.object(
            research_module,
            "validate_bar_series",
            _timed(timings, "validation", research_module.validate_bar_series),
        ),
        patch.object(research_module, "resample_bars", timed_resample),
        patch.object(
            research_module,
            "build_timeframe_context",
            _timed(timings, "timeframe_context", research_module.build_timeframe_context),
        ),
        patch.object(
            research_module,
            "fingerprint_code",
            _timed(timings, "code_fingerprint", research_module.fingerprint_code),
        ),
        patch.object(
            research_module,
            "detect_entry_point_2",
            _timed(timings, "signal_detection", research_module.detect_entry_point_2),
        ),
        patch.object(
            research_module,
            "run_backtest",
            _timed(timings, "backtest", research_module.run_backtest),
        ),
        patch.object(
            research_module,
            "summarize_trades",
            _timed(timings, "metrics", research_module.summarize_trades),
        ),
        patch.object(
            research_module,
            "evaluate_signal_quality",
            _timed(timings, "signal_quality", research_module.evaluate_signal_quality),
        ),
    ):
        started = perf_counter()
        result = research_module.run_research(
            base,
            config,
            "entry_point_2",
            data_fingerprint=data_fingerprint,
            code_root=SOURCE_ROOT / "gold_research",
        )
        timings["run_research_total"] = perf_counter() - started
    return result, timings


def _stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _summary(run) -> dict[str, Any]:
    return {
        "base_bars": len(run.base.bars),
        "context_rows": len(run.context),
        "context_memory_bytes": int(run.context.memory_usage(deep=True).sum()),
        "signals": {
            "count": len(run.signals),
            "sha256": _stable_digest([signal.to_record() for signal in run.signals]),
        },
        "trades": {
            "count": len(run.backtest.trades),
            "sha256": _stable_digest([trade.to_record() for trade in run.backtest.trades]),
        },
        "unfilled_signals": {
            "count": len(run.backtest.unfilled_signals),
            "sha256": _stable_digest(list(run.backtest.unfilled_signals)),
        },
        "metrics": {"sha256": _stable_digest(run.metrics)},
        "signal_quality": {
            "count": len(run.signal_quality),
            "sha256": _stable_digest(list(run.signal_quality)),
        },
    }


def _run_scenario(
    base: BarSeries,
    config,
    data_fingerprint: str,
    runs: int,
) -> tuple[dict[str, Any], Any]:
    measured_runs: list[dict[str, Any]] = []
    last_run = None
    for run_index in range(runs):
        last_run, timings = _run_timed_research(base, config, data_fingerprint)
        measured_runs.append(
            {
                "kind": "cold_start" if run_index == 0 else "same_process_repeat",
                "timings": timings,
                "rss_bytes": _rss_bytes(),
                "summary": _summary(last_run),
            }
        )
    summaries = {
        json.dumps(run["summary"], sort_keys=True, separators=(",", ":"))
        for run in measured_runs
    }
    return (
        {
            "runs": measured_runs,
            "statistics": _timing_stats(measured_runs),
            "stable_output": len(summaries) == 1,
            "summary": measured_runs[0]["summary"],
        },
        last_run,
    )


def _baseline_payload(
    *,
    data_fingerprint: str,
    scenarios: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_commit": BASELINE_SOURCE_COMMIT,
        "benchmark": {"start": START.isoformat(), "end": END.isoformat()},
        "data_fingerprint": data_fingerprint,
        "scenarios": {
            name: {
                **scenario_result["scenario"],
                "summary": scenario_result["summary"],
            }
            for name, scenario_result in scenarios.items()
        },
    }


def _compare_baseline(
    *,
    baseline: dict[str, Any],
    data_fingerprint: str,
    scenarios: dict[str, dict[str, Any]],
    baseline_path: Path,
) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []
    if baseline.get("schema_version") != 1:
        differences.append({"kind": "schema_version", "expected": 1, "actual": baseline.get("schema_version")})
    if baseline.get("source_commit") != BASELINE_SOURCE_COMMIT:
        differences.append(
            {
                "kind": "source_commit",
                "expected": BASELINE_SOURCE_COMMIT,
                "actual": baseline.get("source_commit"),
            }
        )
    expected_benchmark = baseline.get("benchmark", {})
    if expected_benchmark.get("start") != START.isoformat() or expected_benchmark.get("end") != END.isoformat():
        differences.append(
            {
                "kind": "benchmark_range",
                "expected": {"start": START.isoformat(), "end": END.isoformat()},
                "actual": expected_benchmark,
            }
        )
    if baseline.get("data_fingerprint") != data_fingerprint:
        differences.append(
            {
                "kind": "data_fingerprint",
                "expected": baseline.get("data_fingerprint"),
                "actual": data_fingerprint,
            }
        )

    expected_scenarios = baseline.get("scenarios", {})
    expected_names = set(expected_scenarios)
    actual_names = set(scenarios)
    if expected_names != actual_names:
        differences.append(
            {
                "kind": "scenario_names",
                "expected": sorted(expected_names),
                "actual": sorted(actual_names),
            }
        )
    for name in sorted(expected_names & actual_names):
        expected = expected_scenarios[name]
        actual = scenarios[name]
        expected_config = {key: expected.get(key) for key in actual["scenario"]}
        if expected_config != actual["scenario"]:
            differences.append(
                {
                    "kind": "scenario_config",
                    "scenario": name,
                    "expected": expected_config,
                    "actual": actual["scenario"],
                }
            )
        if expected.get("summary") != actual["summary"]:
            differences.append(
                {
                    "kind": "scenario_summary",
                    "scenario": name,
                    "expected": expected.get("summary"),
                    "actual": actual["summary"],
                }
            )
    return {
        "enabled": True,
        "matched": not differences,
        "path": str(baseline_path),
        "differences": differences,
    }


def _timing_stats(runs: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    names = sorted({name for run in runs for name in run["timings"]})
    return {
        name: {
            "median_seconds": median(run["timings"][name] for run in runs if name in run["timings"]),
            "max_seconds": max(run["timings"][name] for run in runs if name in run["timings"]),
        }
        for name in names
    }


def run_benchmark(
    config_path: Path,
    database: Path,
    runs: int,
    *,
    baseline_path: Path | None = DEFAULT_BASELINE_PATH,
    write_baseline: bool = False,
) -> dict[str, Any]:
    if runs < MIN_BENCHMARK_RUNS:
        raise ValueError(f"runs must be at least {MIN_BENCHMARK_RUNS}")
    config = load_config(config_path)
    base, digest, load_seconds = _load_cached(config, database)
    scenario_results: dict[str, dict[str, Any]] = {}
    baseline_last_run = None
    for scenario in BENCHMARK_SCENARIOS:
        scenario_config = _scenario_config(config, scenario)
        scenario_result, last_run = _run_scenario(
            _scenario_base(base, scenario),
            scenario_config,
            digest,
            runs,
        )
        scenario_result["scenario"] = scenario.to_dict()
        scenario_results[scenario.name] = scenario_result
        if scenario.name == "both_mid":
            baseline_last_run = last_run

    with tempfile.TemporaryDirectory(prefix="gold-research-benchmark-") as temporary:
        artifact_started = perf_counter()
        artifact_path = write_run_artifacts(baseline_last_run, Path(temporary))
        artifact_seconds = perf_counter() - artifact_started
        artifact_files = sorted(path.name for path in artifact_path.iterdir() if path.is_file())

    if write_baseline:
        if baseline_path is None:
            raise ValueError("baseline_path is required when write_baseline is true")
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(
                _baseline_payload(data_fingerprint=digest, scenarios=scenario_results),
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        baseline_comparison = {
            "enabled": True,
            "matched": None,
            "path": str(baseline_path),
            "differences": [],
            "mode": "written",
        }
    elif baseline_path is None:
        baseline_comparison = {
            "enabled": False,
            "matched": None,
            "path": "",
            "differences": [],
        }
    else:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_comparison = _compare_baseline(
            baseline=baseline,
            data_fingerprint=digest,
            scenarios=scenario_results,
            baseline_path=baseline_path,
        )

    primary = scenario_results["both_mid"]
    result = {
        "benchmark": {
            "start": START.isoformat(),
            "end": END.isoformat(),
            "config": str(config_path),
            "database": str(database),
            "cache_only": True,
            "runs": runs,
        },
        "load": {
            "seconds": load_seconds,
            "bars": len(base.bars),
            "data_fingerprint": digest,
            "dataframe_memory_bytes": int(base.bars.memory_usage(deep=True).sum()),
        },
        "timings": {"runs": primary["runs"], "statistics": primary["statistics"]},
        "scenarios": scenario_results,
        "artifacts": {
            "seconds": artifact_seconds,
            "files": artifact_files,
        },
        "stable_output": all(scenario["stable_output"] for scenario in scenario_results.values()),
        "baseline_comparison": baseline_comparison,
        "rss_bytes": _rss_bytes(),
    }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "configs" / "xauusd_baseline.toml",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "cache" / "oanda.sqlite3",
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE_PATH)
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.runs < MIN_BENCHMARK_RUNS:
        parser.error(f"--runs must be at least {MIN_BENCHMARK_RUNS}")
    if args.skip_baseline and args.write_baseline:
        parser.error("--skip-baseline and --write-baseline cannot be used together")

    result = run_benchmark(
        args.config,
        args.database,
        args.runs,
        baseline_path=None if args.skip_baseline else args.baseline,
        write_baseline=args.write_baseline,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["stable_output"] and result["baseline_comparison"]["matched"] is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
