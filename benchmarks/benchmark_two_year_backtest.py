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
from gold_research.domain import InstrumentMetadata  # noqa: E402
from gold_research.reporting.artifacts import write_run_artifacts  # noqa: E402


START = datetime(2024, 7, 25, tzinfo=timezone.utc)
END = datetime(2026, 7, 25, tzinfo=timezone.utc)


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
) -> dict[str, Any]:
    config = load_config(config_path)
    base, digest, load_seconds = _load_cached(config, database)
    measured_runs: list[dict[str, Any]] = []
    last_run = None
    for run_index in range(runs):
        last_run, timings = _run_timed_research(base, config, digest)
        measured_runs.append(
            {
                "kind": "cold_start" if run_index == 0 else "same_process_repeat",
                "timings": timings,
                "rss_bytes": _rss_bytes(),
                "summary": _summary(last_run),
            }
        )

    with tempfile.TemporaryDirectory(prefix="gold-research-benchmark-") as temporary:
        artifact_started = perf_counter()
        artifact_path = write_run_artifacts(last_run, Path(temporary))
        artifact_seconds = perf_counter() - artifact_started
        artifact_files = sorted(path.name for path in artifact_path.iterdir() if path.is_file())

    summaries = {
        json.dumps(run["summary"], sort_keys=True, separators=(",", ":"))
        for run in measured_runs
    }
    return {
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
        "timings": {
            "runs": measured_runs,
            "statistics": _timing_stats(measured_runs),
        },
        "artifacts": {
            "seconds": artifact_seconds,
            "files": artifact_files,
        },
        "stable_output": len(summaries) == 1,
        "rss_bytes": _rss_bytes(),
    }


def main() -> int:
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
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")

    result = run_benchmark(args.config, args.database, args.runs)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
