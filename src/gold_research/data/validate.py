"""Data-quality checks for canonical OHLC bar series."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..domain import BarSeries, DataQualityIssue, REQUIRED_OHLC_COLUMNS
from ..market_calendar import NO_MARKET_CALENDAR, unexpected_missing_bar_ranges
from .normalize import _time_delta


@dataclass
class DataValidationError(ValueError):
    """Raised when one or more data contract checks fail."""

    issues: list[DataQualityIssue]

    def __str__(self) -> str:
        return "; ".join(f"{issue.code}: {issue.message}" for issue in self.issues)


def fatal_data_issues(
    issues: list[DataQualityIssue],
    missing_bar_policy: str,
    max_gap_bars: int,
) -> list[DataQualityIssue]:
    """Apply the configured gap policy without hiding other quality errors."""

    if missing_bar_policy == "block":
        return list(issues)
    return [
        issue
        for issue in issues
        if issue.code != "MISSING_BARS"
        or missing_bar_policy == "warn" and issue.count > max_gap_bars
    ]


def _issue(code: str, message: str, **kwargs: object) -> DataQualityIssue:
    return DataQualityIssue(code=code, severity="error", message=message, **kwargs)


def validate_bar_series(
    series: BarSeries,
    expected_interval: str | None = None,
    closed_weekdays: tuple[int, ...] = (),
    market_calendar: str = NO_MARKET_CALENDAR,
    raise_on_error: bool = True,
) -> list[DataQualityIssue]:
    """Validate timestamps, OHLC relationships, and unfilled data gaps."""

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
        gap_rows = []
        for index in range(1, len(timestamps)):
            previous = pd.Timestamp(timestamps.iloc[index - 1])
            current = pd.Timestamp(timestamps.iloc[index])
            if current - previous > interval:
                gap_rows.extend(
                    unexpected_missing_bar_ranges(
                        previous,
                        current,
                        interval,
                        market_calendar=market_calendar,
                        closed_weekdays=closed_weekdays,
                    )
                )
        for start, end, count in gap_rows:
            issues.append(
                _issue(
                    "MISSING_BARS",
                    f"{count} expected bar(s) missing between {start.isoformat()} and {end.isoformat()}",
                    start=start.isoformat(),
                    end=end.isoformat(),
                    count=count,
                )
            )

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
