"""UTC-aligned OHLC resampling without manufacturing incomplete bars."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ..domain import BarSeries
from .normalize import DataNormalizationError, _time_delta


def resample_bars(source: BarSeries, target_timeframe: str) -> BarSeries:
    """Aggregate complete source bars into UTC-aligned target bars."""

    source_delta = _time_delta(source.timeframe)
    target_delta = _time_delta(target_timeframe)
    if target_delta < source_delta:
        raise DataNormalizationError("target timeframe must not be smaller than source timeframe")
    if target_delta % source_delta != pd.Timedelta(0):
        raise DataNormalizationError("target timeframe must be an integer multiple of source timeframe")
    if target_delta == source_delta:
        return source.copy()
    expected_count = int(target_delta / source_delta)
    frame = source.bars.copy()
    if frame.empty:
        return BarSeries(
            bars=frame,
            timeframe=target_timeframe,
            metadata=replace(source.metadata, source_interval=target_timeframe),
            quality_issues=list(source.quality_issues),
        )

    indexed = frame.set_index("open_time")
    aggregate = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in indexed:
        aggregate["volume"] = "sum"
    grouped = indexed.resample(
        target_timeframe,
        origin="epoch",
        label="left",
        closed="left",
    ).agg(aggregate)
    counts = indexed["close"].resample(
        target_timeframe,
        origin="epoch",
        label="left",
        closed="left",
    ).count()
    grouped = grouped[counts == expected_count].dropna(subset=["open", "high", "low", "close"])
    grouped = grouped.reset_index()
    grouped["close_time"] = grouped["open_time"] + target_delta
    columns = ["open_time", "close_time", "open", "high", "low", "close"]
    if "volume" in grouped:
        columns.append("volume")
    return BarSeries(
        bars=grouped[columns],
        timeframe=target_timeframe,
        metadata=replace(source.metadata, source_interval=target_timeframe),
        quality_issues=list(source.quality_issues),
    )

