"""Causal alignment of multi-timeframe trend state to base-bar closes."""

from __future__ import annotations

import pandas as pd

from ..domain import BarSeries
from ..config import TrendConfig
from .indicators import add_indicators, trend_state


def _with_trend(frame: pd.DataFrame, config: TrendConfig, atr_period: int | None = None) -> pd.DataFrame:
    result = add_indicators(
        frame,
        ema_fast=config.ema_fast,
        ema_slow=config.ema_slow,
        slope_lookback=config.slope_lookback,
        atr_period=atr_period,
    )
    result["trend"] = trend_state(result)
    result["source_close_time"] = result["close_time"]
    return result


def _align_completed(
    base: pd.DataFrame,
    higher: pd.DataFrame,
    prefix: str,
) -> pd.DataFrame:
    right = higher[["close_time", "trend", "source_close_time"]].rename(
        columns={
            "trend": f"{prefix}_trend",
            "source_close_time": f"{prefix}_source_close_time",
        }
    )
    left = base.sort_values("close_time").copy()
    right = right.sort_values("close_time").copy()
    aligned = pd.merge_asof(
        left,
        right,
        on="close_time",
        direction="backward",
        allow_exact_matches=True,
    )
    return aligned.sort_index()


def build_timeframe_context(
    base: BarSeries,
    medium: BarSeries,
    large: BarSeries,
    trend_config: TrendConfig,
    *,
    atr_period: int | None = None,
) -> pd.DataFrame:
    """Build one row per base bar with only completed higher-timeframe state."""

    base_frame = _with_trend(base.bars, trend_config, atr_period=atr_period)
    medium_frame = _with_trend(medium.bars, trend_config)
    large_frame = _with_trend(large.bars, trend_config)
    result = base_frame.rename(
        columns={
            "trend": "base_trend",
            "source_close_time": "base_source_close_time",
        }
    )
    result = _align_completed(result, medium_frame, "medium")
    result = _align_completed(result, large_frame, "large")
    result["signal_time"] = result["close_time"]
    for column in ("base_trend", "medium_trend", "large_trend"):
        result[column] = result[column].fillna("unknown").astype("string")
    result["all_up"] = (
        (result["base_trend"] == "up")
        & (result["medium_trend"] == "up")
        & (result["large_trend"] == "up")
    )
    result["all_down"] = (
        (result["base_trend"] == "down")
        & (result["medium_trend"] == "down")
        & (result["large_trend"] == "down")
    )
    return result.reset_index(drop=True)

