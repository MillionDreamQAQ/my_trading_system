"""Fresh breakout entry-point-2 signal engine."""

from __future__ import annotations

import pandas as pd

from ..config import ResearchConfig
from ..domain import Direction, Signal


def _signal_from_row(
    row: pd.Series,
    side: Direction,
    breakout_level: float,
    entry_time: pd.Timestamp | None,
    lookback: int,
) -> Signal:
    return Signal(
        strategy_id="entry_point_2",
        side=side,
        signal_time=pd.Timestamp(row["signal_time"]),
        entry_time=entry_time,
        breakout_level=float(breakout_level),
        atr=None if pd.isna(row.get("atr")) else float(row["atr"]),
        reason=f"fresh_close_breakout_above_{lookback}_bar_high"
        if side is Direction.LONG
        else f"fresh_close_breakout_below_{lookback}_bar_low",
        base_trend=str(row["base_trend"]),
        medium_trend=str(row["medium_trend"]),
        large_trend=str(row["large_trend"]),
        medium_source_close_time=(
            None if pd.isna(row.get("medium_source_close_time")) else pd.Timestamp(row["medium_source_close_time"])
        ),
        large_source_close_time=(
            None if pd.isna(row.get("large_source_close_time")) else pd.Timestamp(row["large_source_close_time"])
        ),
    )


def detect_entry_point_2(context: pd.DataFrame, config: ResearchConfig) -> list[Signal]:
    """Detect signals at the current base-bar close without position filtering."""

    if not config.entry_point_2.enabled:
        return []
    lookback = config.entry_point_2.breakout_lookback
    frame = context.copy().reset_index(drop=True)
    frame["long_level"] = frame["high"].shift(1).rolling(lookback, min_periods=lookback).max()
    frame["short_level"] = frame["low"].shift(1).rolling(lookback, min_periods=lookback).min()
    if frame.empty:
        return []
    signals: list[Signal] = []
    length = len(frame)
    long_allowed = config.direction in {Direction.LONG, Direction.BOTH}
    short_allowed = config.direction in {Direction.SHORT, Direction.BOTH}
    open_times = frame["open_time"].tolist() if length > 1 else []
    closes = frame["close"].to_numpy(copy=False) if "close" in frame else None
    long_levels = frame["long_level"].to_numpy(copy=False)
    short_levels = frame["short_level"].to_numpy(copy=False)
    all_up = frame["all_up"].to_numpy(copy=False) if long_allowed else None
    all_down = frame["all_down"].to_numpy(copy=False) if short_allowed else None

    for index in range(length):
        long_level = long_levels[index]
        short_level = short_levels[index]
        next_entry = pd.Timestamp(open_times[index + 1]) if index + 1 < length else None
        previous_close = (
            None
            if index == 0
            else closes[index - 1] if closes is not None else frame.iloc[index - 1]["close"]
        )
        previous_long_level = long_levels[index - 1] if index > 0 else None
        previous_short_level = short_levels[index - 1] if index > 0 else None
        if long_allowed and bool(all_up[index]) and pd.notna(long_level):
            close = closes[index] if closes is not None else frame.iloc[index]["close"]
            fresh = close > long_level and (
                previous_close is None
                or pd.isna(previous_long_level)
                or previous_close <= previous_long_level
            )
            if fresh:
                signals.append(
                    _signal_from_row(frame.iloc[index], Direction.LONG, long_level, next_entry, lookback)
                )
        if short_allowed and bool(all_down[index]) and pd.notna(short_level):
            close = closes[index] if closes is not None else frame.iloc[index]["close"]
            fresh = close < short_level and (
                previous_close is None
                or pd.isna(previous_short_level)
                or previous_close >= previous_short_level
            )
            if fresh:
                signals.append(
                    _signal_from_row(frame.iloc[index], Direction.SHORT, short_level, next_entry, lookback)
                )
    return signals
