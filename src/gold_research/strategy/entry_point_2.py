"""Fresh breakout entry-point-2 signal engine."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import ResearchConfig
from ..domain import Direction, Signal


_NAT_NS = np.iinfo(np.int64).min


def _datetime_values(series: pd.Series) -> tuple[object, object | None]:
    values = series.array
    if isinstance(values, pd.arrays.DatetimeArray):
        if values.tz is None:
            return values, None
        return values.asi8, values.tz
    return values, None


def _timestamp_at(
    values: object,
    index: int,
    timezone: object | None,
    *,
    optional: bool = False,
) -> pd.Timestamp | None:
    value = values[index]
    if timezone is not None:
        if int(value) == _NAT_NS:
            return None if optional else pd.NaT
        return pd.Timestamp(int(value), unit="ns", tz=timezone)
    if optional and (value is None or pd.isna(value)):
        return None
    return pd.Timestamp(value)


def _signal_from_values(
    signal_time: pd.Timestamp,
    side: Direction,
    breakout_level: float,
    entry_time: pd.Timestamp | None,
    lookback: int,
    atr_value: float | None,
    base_trend: str,
    medium_trend: str,
    large_trend: str,
    medium_source_close_time: pd.Timestamp | None,
    large_source_close_time: pd.Timestamp | None,
) -> Signal:
    return Signal(
        strategy_id="entry_point_2",
        side=side,
        signal_time=signal_time,
        entry_time=entry_time,
        breakout_level=float(breakout_level),
        atr=atr_value,
        reason=f"fresh_close_breakout_above_{lookback}_bar_high"
        if side is Direction.LONG
        else f"fresh_close_breakout_below_{lookback}_bar_low",
        base_trend=base_trend,
        medium_trend=medium_trend,
        large_trend=large_trend,
        medium_source_close_time=medium_source_close_time,
        large_source_close_time=large_source_close_time,
    )


def detect_entry_point_2(context: pd.DataFrame, config: ResearchConfig) -> list[Signal]:
    """Detect signals at the current base-bar close without position filtering."""

    if not config.entry_point_2.enabled:
        return []
    if context.empty:
        return []
    lookback = config.entry_point_2.breakout_lookback
    length = len(context)
    long_level_values = (
        context["high"].shift(1).rolling(lookback, min_periods=lookback).max().to_numpy(copy=False)
    )
    short_level_values = (
        context["low"].shift(1).rolling(lookback, min_periods=lookback).min().to_numpy(copy=False)
    )
    long_allowed = config.direction in {Direction.LONG, Direction.BOTH}
    short_allowed = config.direction in {Direction.SHORT, Direction.BOTH}
    all_up = (
        context["all_up"].fillna(False).to_numpy(dtype=bool, copy=False)
        if long_allowed
        else np.zeros(length, dtype=bool)
    )
    all_down = (
        context["all_down"].fillna(False).to_numpy(dtype=bool, copy=False)
        if short_allowed
        else np.zeros(length, dtype=bool)
    )

    possible = (all_up & pd.notna(long_level_values)) | (all_down & pd.notna(short_level_values))
    if not possible.any():
        return []

    closes = context["close"].to_numpy(copy=False)
    long_mask = long_allowed & all_up & pd.notna(long_level_values) & (closes > long_level_values)
    short_mask = short_allowed & all_down & pd.notna(short_level_values) & (closes < short_level_values)
    if length > 1:
        long_mask[1:] &= pd.isna(long_level_values[:-1]) | (closes[:-1] <= long_level_values[:-1])
        short_mask[1:] &= pd.isna(short_level_values[:-1]) | (closes[:-1] >= short_level_values[:-1])

    candidate_indices = np.flatnonzero(long_mask | short_mask)
    if candidate_indices.size == 0:
        return []

    signal_times, signal_time_zone = _datetime_values(context["signal_time"])
    base_trends = context["base_trend"].to_numpy(copy=False)
    medium_trends = context["medium_trend"].to_numpy(copy=False)
    large_trends = context["large_trend"].to_numpy(copy=False)
    open_times = None
    open_time_zone = None
    if np.any(candidate_indices + 1 < length):
        open_times, open_time_zone = _datetime_values(context["open_time"])
    atr_values = context["atr"].to_numpy(copy=False) if "atr" in context else None
    medium_sources, medium_source_zone = (
        _datetime_values(context["medium_source_close_time"])
        if "medium_source_close_time" in context
        else (None, None)
    )
    large_sources, large_source_zone = (
        _datetime_values(context["large_source_close_time"])
        if "large_source_close_time" in context
        else (None, None)
    )

    signals: list[Signal] = []
    for index in candidate_indices:
        next_entry = (
            None
            if index + 1 >= length
            else _timestamp_at(open_times, index + 1, open_time_zone)
        )
        signal_time = _timestamp_at(signal_times, index, signal_time_zone)
        raw_atr = atr_values[index] if atr_values is not None else None
        atr_value = None if raw_atr is None or pd.isna(raw_atr) else float(raw_atr)
        base_trend = str(base_trends[index])
        medium_trend = str(medium_trends[index])
        large_trend = str(large_trends[index])
        medium_source = (
            _timestamp_at(medium_sources, index, medium_source_zone, optional=True)
            if medium_sources is not None
            else None
        )
        large_source = (
            _timestamp_at(large_sources, index, large_source_zone, optional=True)
            if large_sources is not None
            else None
        )
        if long_mask[index]:
            signals.append(
                _signal_from_values(
                    signal_time,
                    Direction.LONG,
                    long_level_values[index],
                    next_entry,
                    lookback,
                    atr_value,
                    base_trend,
                    medium_trend,
                    large_trend,
                    medium_source,
                    large_source,
                )
            )
        if short_mask[index]:
            signals.append(
                _signal_from_values(
                    signal_time,
                    Direction.SHORT,
                    short_level_values[index],
                    next_entry,
                    lookback,
                    atr_value,
                    base_trend,
                    medium_trend,
                    large_trend,
                    medium_source,
                    large_source,
                )
            )
    return signals
