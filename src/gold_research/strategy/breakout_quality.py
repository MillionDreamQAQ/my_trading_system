"""Causal filters for the quality of a breakout bar.

Every rolling window is shifted so the bar being evaluated is never included
in the reference sample.  The returned masks are directional because a good
long breakout closes near its high while a good short breakout closes near its
low.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import EntryPoint2Config


_VOLUME_LOOKBACK = 20
_BOLLINGER_PERIOD = 20
_BOLLINGER_STDDEV = 2.0


def _numeric_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[name], errors="coerce")


def _historical_squeeze(
    values: pd.Series,
    *,
    lookback: int,
    recent_bars: int,
    percentile: float,
) -> pd.Series:
    """Return whether the recent average is below its prior distribution."""

    prior = values.shift(1)
    recent_average = prior.rolling(recent_bars, min_periods=recent_bars).mean()
    # Keep the reference distribution before the recent window. This makes
    # the comparison stable even when the recent bars are already expanding.
    historical = prior.shift(recent_bars).rolling(lookback, min_periods=lookback).quantile(percentile)
    return (recent_average <= historical).fillna(False)


def _squeeze_masks(frame: pd.DataFrame, config: EntryPoint2Config) -> np.ndarray:
    atr = _numeric_column(frame, "atr")
    atr_squeeze = _historical_squeeze(
        atr,
        lookback=config.squeeze_lookback,
        recent_bars=config.squeeze_recent_bars,
        percentile=config.squeeze_percentile,
    )

    close = _numeric_column(frame, "close")
    middle = close.rolling(_BOLLINGER_PERIOD, min_periods=_BOLLINGER_PERIOD).mean()
    deviation = close.rolling(_BOLLINGER_PERIOD, min_periods=_BOLLINGER_PERIOD).std(ddof=0)
    bandwidth = (2.0 * _BOLLINGER_STDDEV * deviation / middle.abs()).replace([np.inf, -np.inf], np.nan)
    bandwidth_squeeze = _historical_squeeze(
        bandwidth,
        lookback=config.squeeze_lookback,
        recent_bars=config.squeeze_recent_bars,
        percentile=config.squeeze_percentile,
    )
    return (atr_squeeze | bandwidth_squeeze).to_numpy(dtype=bool, copy=False)


def breakout_quality_masks(
    frame: pd.DataFrame,
    config: EntryPoint2Config,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(long, short)`` masks for the configured quality filters."""

    length = len(frame)
    long_mask = np.ones(length, dtype=bool)
    short_mask = np.ones(length, dtype=bool)

    if config.volume_filter_enabled:
        volume = _numeric_column(frame, "volume")
        average = volume.shift(1).rolling(_VOLUME_LOOKBACK, min_periods=_VOLUME_LOOKBACK).mean()
        volume_ok = (volume > average * config.volume_multiplier).fillna(False).to_numpy(dtype=bool, copy=False)
        long_mask &= volume_ok
        short_mask &= volume_ok

    if config.kline_quality_enabled:
        opening = _numeric_column(frame, "open")
        high = _numeric_column(frame, "high")
        low = _numeric_column(frame, "low")
        close = _numeric_column(frame, "close")
        candle_range = high - low
        valid_range = candle_range > 0
        body_ratio = (close - opening).abs() / candle_range
        long_close_distance = (high - close) / candle_range
        short_close_distance = (close - low) / candle_range
        body_ok = (body_ratio >= config.min_body_ratio).fillna(False)
        long_close_ok = (long_close_distance <= config.max_close_extreme_ratio).fillna(False)
        short_close_ok = (short_close_distance <= config.max_close_extreme_ratio).fillna(False)
        long_mask &= (valid_range & body_ok & long_close_ok).to_numpy(dtype=bool, copy=False)
        short_mask &= (valid_range & body_ok & short_close_ok).to_numpy(dtype=bool, copy=False)

    if config.squeeze_filter_enabled:
        squeeze_ok = _squeeze_masks(frame, config)
        long_mask &= squeeze_ok
        short_mask &= squeeze_ok

    return long_mask, short_mask
