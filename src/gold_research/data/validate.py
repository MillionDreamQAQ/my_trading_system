"""Data-quality checks for canonical OHLC bar series."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..domain import BarSeries, DataQualityIssue, REQUIRED_OHLC_COLUMNS
from .normalize import _time_delta


@dataclass
class DataValidationError(ValueError):
    """Raised when one or more data contract checks fail."""

    issues: list[DataQualityIssue]

    def __str__(self) -> str:
        return "; ".join(f"{issue.code}: {issue.message}" for issue in self.issues)


def _issue(code: str, message: str, **kwargs: object) -> DataQualityIssue:
    return DataQualityIssue(code=code, severity="error", message=message, **kwargs)


def validate_bar_series(
    series: BarSeries,
    expected_interval: str | None = None,
    raise_on_error: bool = True,
) -> list[DataQualityIssue]:
    """Validate timestamps, OHLC relationships, and price values.

    OANDA's historical-candle response is the source of truth for whether a
    market interval traded. Gaps between returned candles are not inferred as
    invalid local data.
    """

    frame = series.bars
    interval = _time_delta(expected_interval or series.timeframe)
    issues: list[DataQualityIssue] = []
    missing_columns = [column for column in REQUIRED_OHLC_COLUMNS if column not in frame.columns]
    if missing_columns:
        issues.append(_issue("MISSING_COLUMNS", f"missing columns: {', '.join(missing_columns)}"))
        if raise_on_error:
            raise DataValidationError(issues)
        return issues

    timestamps = frame["open_time"]
    if timestamps.isna().any() or not isinstance(timestamps.dtype, pd.DatetimeTZDtype):
        issues.append(_issue("INVALID_TIMESTAMP", "open_time must be non-null timezone-aware datetimes"))
    else:
        if timestamps.duplicated().any():
            issues.append(_issue("DUPLICATE_TIMESTAMP", "open_time contains duplicate timestamps"))
        if not timestamps.is_monotonic_increasing:
            issues.append(_issue("UNSORTED_TIMESTAMP", "open_time must be strictly increasing"))
        differences = timestamps.diff().dropna()
        if (differences <= pd.Timedelta(0)).any():
            issues.append(_issue("NON_INCREASING_TIMESTAMP", "open_time must be strictly increasing"))
        close_expected = timestamps + interval
        if not frame["close_time"].eq(close_expected).all():
            issues.append(_issue("INVALID_CLOSE_TIME", "close_time must equal open_time plus the bar interval"))
    for column in ("open", "high", "low", "close"):
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any():
            issues.append(_issue("INVALID_PRICE", f"{column} contains non-numeric or missing values"))
        elif (values <= 0).any():
            issues.append(_issue("NON_POSITIVE_PRICE", f"{column} must contain only positive values"))
    if not frame[["open", "high", "low", "close"]].isna().any().any():
        invalid_high = (frame["high"] < frame[["open", "close"]].max(axis=1)).any()
        invalid_low = (frame["low"] > frame[["open", "close"]].min(axis=1)).any()
        if invalid_high or invalid_low:
            issues.append(_issue("INVALID_OHLC", "high/low do not contain the open and close values"))
    if "volume" in frame and frame["volume"].notna().any() and (frame["volume"].dropna() < 0).any():
        issues.append(_issue("INVALID_VOLUME", "volume cannot be negative"))

    if issues and raise_on_error:
        raise DataValidationError(issues)
    return issues
