"""Write a complete, deterministic research output directory."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from ..research import ResearchRun
from .markdown_report import render_markdown_report


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, records: Iterable[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fields})


def write_run_artifacts(run: ResearchRun, output_root: str | Path = "runs") -> Path:
    output_dir = Path(output_root) / run.manifest.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "manifest.json", run.manifest.to_dict())
    _write_json(output_dir / "metrics.json", run.metrics)
    _write_json(output_dir / "warnings.json", [issue.to_dict() for issue in run.warnings])
    _write_csv(
        output_dir / "signals.csv",
        (signal.to_record() for signal in run.signals),
        [
            "strategy_id", "side", "signal_time", "entry_time", "breakout_level", "atr", "reason",
            "base_trend", "medium_trend", "large_trend", "medium_source_close_time",
            "large_source_close_time", "setup_id",
        ],
    )
    _write_csv(
        output_dir / "signal_quality.csv",
        run.signal_quality,
        ["strategy_id", "side", "signal_time", "window_bars", "signal_close", "forward_return", "mfe", "mae"],
    )
    _write_csv(
        output_dir / "trades.csv",
        (trade.to_record() for trade in run.backtest.trades),
        [
            "strategy_id", "side", "signal_time", "entry_time", "exit_time", "entry_price", "exit_price",
            "quantity", "stop_price", "target_price", "exit_reason", "gross_pnl", "spread_cost",
            "slippage_cost", "commission", "net_pnl", "r_multiple", "mfe", "mae", "hold_bars",
        ],
    )
    _write_json(output_dir / "unfilled_signals.json", list(run.backtest.unfilled_signals))
    if run.setup_result is not None:
        _write_json(
            output_dir / "setups.json",
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
                for setup in run.setup_result.setups
            ],
        )
    (output_dir / "report.md").write_text(render_markdown_report(run), encoding="utf-8")
    return output_dir
