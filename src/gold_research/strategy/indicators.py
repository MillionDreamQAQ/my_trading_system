"""Causal indicators calculated only from bars that have already closed."""

from __future__ import annotations

import pandas as pd


def exponential_moving_average(values: pd.Series, period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    return values.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()


def average_true_range(frame: pd.DataFrame, period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=period).mean()


def add_indicators(
    frame: pd.DataFrame,
    ema_fast: int,
    ema_slow: int,
    slope_lookback: int,
    atr_period: int | None = None,
) -> pd.DataFrame:
    """Return a frame with indicators; input rows and ordering are unchanged."""

    result = frame.copy()
    result["ema_fast"] = exponential_moving_average(result["close"], ema_fast)
    result["ema_slow"] = exponential_moving_average(result["close"], ema_slow)
    result["ema_slow_slope"] = result["ema_slow"] - result["ema_slow"].shift(slope_lookback)
    if atr_period is not None:
        result["atr"] = average_true_range(result, atr_period)
    return result


def trend_state(frame: pd.DataFrame) -> pd.Series:
    """Classify each closed bar as up, down, or unknown."""

    valid = frame[["ema_fast", "ema_slow", "ema_slow_slope", "close"]].notna().all(axis=1)
    up = valid & (frame["ema_fast"] > frame["ema_slow"]) & (frame["ema_slow_slope"] > 0) & (
        frame["close"] > frame["ema_fast"]
    )
    down = valid & (frame["ema_fast"] < frame["ema_slow"]) & (frame["ema_slow_slope"] < 0) & (
        frame["close"] < frame["ema_fast"]
    )
    result = pd.Series("unknown", index=frame.index, dtype="string")
    result.loc[up] = "up"
    result.loc[down] = "down"
    return result

