"""End-to-end historical research orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd

from .backtest.execution import BacktestResult, run_backtest
from .backtest.metrics import summarize_trades
from .config import ResearchConfig, validate_oanda_xauusd_config
from .data.resample import resample_bars
from .data.validate import DataValidationError, validate_bar_series
from .domain import BarSeries, DataQualityIssue, RunManifest, Signal
from .strategy.entry_point_2 import detect_entry_point_2
from .strategy.entry_point_3 import EntryPoint3Result, detect_entry_point_3
from .strategy.timeframe_context import build_timeframe_context


@dataclass(frozen=True)
class ResearchRun:
    strategy_id: str
    manifest: RunManifest
    base: BarSeries
    medium: BarSeries
    large: BarSeries
    context: pd.DataFrame
    signals: tuple[Signal, ...]
    backtest: BacktestResult
    metrics: dict[str, Any]
    warnings: tuple[DataQualityIssue, ...]
    setup_result: EntryPoint3Result | None = None
    signal_quality: tuple[dict[str, Any], ...] = ()


def fingerprint_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_code(root: str | Path) -> str:
    digest = hashlib.sha256()
    root_path = Path(root)
    for path in sorted(root_path.rglob("*.py")):
        digest.update(path.relative_to(root_path).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint_bars(series: BarSeries) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(series.metadata.to_dict(), sort_keys=True).encode("utf-8"))
    digest.update(series.timeframe.encode("utf-8"))
    digest.update(series.bars.to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%S%z").encode("utf-8"))
    return digest.hexdigest()


def _stable_run_id(
    strategy_id: str,
    config: ResearchConfig,
    data_fingerprint: str,
    code_fingerprint: str,
    base: BarSeries,
) -> str:
    payload = json.dumps(
        {
            "strategy": strategy_id,
            "config": config.fingerprint(),
            "data": data_fingerprint,
            "code": code_fingerprint,
            "start": base.start.isoformat() if base.start is not None else "",
            "end": base.end.isoformat() if base.end is not None else "",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _as_warning(code: str, message: str) -> DataQualityIssue:
    return DataQualityIssue(code=code, severity="warning", message=message)


def _validate_research_contract(base: BarSeries, config: ResearchConfig) -> None:
    """Require configuration and loaded bars to share one OANDA contract."""

    validate_oanda_xauusd_config(config)
    metadata = base.metadata
    source_config = replace(
        config,
        instrument=replace(
            config.instrument,
            symbol=metadata.symbol,
            provider=metadata.provider,
            price_basis=metadata.price_basis,
            timezone=metadata.source_timezone,
            venue=metadata.venue,
            contract_unit=metadata.contract_unit,
            quote_currency=metadata.quote_currency,
            tick_size=metadata.tick_size,
            point_value=metadata.point_value,
        ),
    )
    validate_oanda_xauusd_config(source_config)
    if source_config.instrument != config.instrument:
        raise ValueError("loaded OANDA metadata does not match the configured instrument contract")


def evaluate_signal_quality(
    series: BarSeries,
    signals: tuple[Signal, ...] | list[Signal],
    windows: tuple[int, ...] = (5, 10, 20, 40),
) -> tuple[dict[str, Any], ...]:
    """Measure fixed future windows separately from executable trades."""

    bars = series.bars.reset_index(drop=True)
    close_to_index = {pd.Timestamp(value): index for index, value in enumerate(bars["close_time"])}
    records: list[dict[str, Any]] = []
    for signal in signals:
        index = close_to_index.get(pd.Timestamp(signal.signal_time))
        if index is None:
            continue
        entry_close = float(bars.loc[index, "close"])
        for window in windows:
            end = index + window
            if window <= 0 or end >= len(bars):
                continue
            future = bars.iloc[index + 1 : end + 1]
            if signal.side.value == "long":
                forward_return = float(bars.loc[end, "close"]) / entry_close - 1.0
                mfe = max(0.0, float(future["high"].max()) / entry_close - 1.0)
                mae = max(0.0, 1.0 - float(future["low"].min()) / entry_close)
            else:
                forward_return = 1.0 - float(bars.loc[end, "close"]) / entry_close
                mfe = max(0.0, 1.0 - float(future["low"].min()) / entry_close)
                mae = max(0.0, float(future["high"].max()) / entry_close - 1.0)
            records.append(
                {
                    "strategy_id": signal.strategy_id,
                    "side": signal.side.value,
                    "signal_time": pd.Timestamp(signal.signal_time).isoformat(),
                    "window_bars": window,
                    "signal_close": entry_close,
                    "forward_return": forward_return,
                    "mfe": mfe,
                    "mae": mae,
                }
            )
    return tuple(records)


def run_research(
    base: BarSeries,
    config: ResearchConfig,
    strategy_id: str,
    *,
    data_fingerprint: str | None = None,
    code_root: str | Path | None = None,
) -> ResearchRun:
    """Run one strategy with one data slice and one cost configuration."""

    if strategy_id not in {"entry_point_2", "entry_point_3"}:
        raise ValueError("strategy_id must be entry_point_2 or entry_point_3")
    _validate_research_contract(base, config)
    issues = validate_bar_series(
        base,
        expected_interval=config.timeframes.base,
        raise_on_error=False,
    )
    base.quality_issues.extend(issue for issue in issues if issue not in base.quality_issues)
    if issues:
        raise DataValidationError(issues)
    medium = resample_bars(base, config.timeframes.medium)
    large = resample_bars(base, config.timeframes.large)
    context = build_timeframe_context(
        base,
        medium,
        large,
        config.trend,
        atr_period=config.risk.atr_period,
    )
    data_hash = data_fingerprint or fingerprint_bars(base)
    code_hash = fingerprint_code(code_root or Path(__file__).resolve().parent)
    setup_result = None
    if strategy_id == "entry_point_2":
        signals = tuple(detect_entry_point_2(context, config))
    else:
        setup_result = detect_entry_point_3(context, config)
        signals = setup_result.signals
    backtest = run_backtest(base, signals, config)
    metrics = summarize_trades(backtest.trades)
    metrics["signal_count"] = len(signals)
    metrics["unfilled_signal_count"] = len(backtest.unfilled_signals)
    warnings: list[DataQualityIssue] = list(base.quality_issues)
    if config.costs.spread_value == 0 and config.costs.slippage_value == 0 and config.costs.commission_per_unit == 0:
        warnings.append(_as_warning("ZERO_COST_MODEL", "zero spread, slippage, and commission are suitable only for logic tests"))
    if base.metadata.provider.lower() in {"", "replace-with-provider", "test"}:
        warnings.append(_as_warning("UNVERIFIED_SOURCE_METADATA", "provider metadata is a placeholder or test value"))
    source_is_usable = not any(issue.code == "UNVERIFIED_SOURCE_METADATA" for issue in warnings)
    if base.start is None or base.end is None:
        input_start = input_end = ""
    else:
        input_start, input_end = base.start.isoformat(), base.end.isoformat()
    manifest = RunManifest(
        run_id=_stable_run_id(strategy_id, config, data_hash, code_hash, base),
        strategy_id=strategy_id,
        strategy_version=config.strategy_version,
        config_fingerprint=config.fingerprint(),
        data_fingerprint=data_hash,
        code_fingerprint=code_hash,
        symbol=base.metadata.symbol,
        price_basis=base.metadata.price_basis.value,
        input_start=input_start,
        input_end=input_end,
        timezone=config.timeframes.timezone,
        cost_model=config.to_dict()["costs"],
        warnings=tuple(issue.to_dict() for issue in warnings),
        source_metadata=base.metadata.to_dict(),
        research_usable=source_is_usable,
    )
    return ResearchRun(
        strategy_id=strategy_id,
        manifest=manifest,
        base=base,
        medium=medium,
        large=large,
        context=context,
        signals=signals,
        backtest=backtest,
        metrics=metrics,
        warnings=tuple(warnings),
        setup_result=setup_result,
        signal_quality=evaluate_signal_quality(base, signals),
    )
