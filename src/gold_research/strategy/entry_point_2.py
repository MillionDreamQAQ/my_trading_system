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
) -> Signal:
    return Signal(
        strategy_id="entry_point_2",
        side=side,
        signal_time=pd.Timestamp(row["signal_time"]),
        entry_time=entry_time,
        breakout_level=float(breakout_level),
        atr=None if pd.isna(row.get("atr")) else float(row["atr"]),
        reason="fresh_close_breakout_above_20_bar_high"
        if side is Direction.LONG
        else "fresh_close_breakout_below_20_bar_low",
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
    frame["previous_long_level"] = frame["long_level"].shift(1)
    frame["previous_short_level"] = frame["short_level"].shift(1)
    signals: list[Signal] = []
    for index, row in frame.iterrows():
        next_entry = None
        if index + 1 < len(frame):
            next_entry = pd.Timestamp(frame.loc[index + 1, "open_time"])
        long_allowed = config.direction in {Direction.LONG, Direction.BOTH}
        short_allowed = config.direction in {Direction.SHORT, Direction.BOTH}
        previous_close = frame.loc[index - 1, "close"] if index > 0 else None
        if long_allowed and bool(row["all_up"]) and pd.notna(row["long_level"]):
            fresh = row["close"] > row["long_level"] and (
                previous_close is None or previous_close <= row["long_level"]
            )
            if fresh:
                signals.append(_signal_from_row(row, Direction.LONG, row["long_level"], next_entry))
        if short_allowed and bool(row["all_down"]) and pd.notna(row["short_level"]):
            fresh = row["close"] < row["short_level"] and (
                previous_close is None or previous_close >= row["short_level"]
            )
            if fresh:
                signals.append(_signal_from_row(row, Direction.SHORT, row["short_level"], next_entry))
    return signals
