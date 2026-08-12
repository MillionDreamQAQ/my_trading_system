"""Convert provider-specific OHLC frames into the internal UTC contract."""

from __future__ import annotations

from dataclasses import replace

import pandas as pd

from ..domain import BarSeries, InstrumentMetadata


class DataNormalizationError(ValueError):
    """Raised when raw data cannot be converted to the canonical shape."""


_COLUMN_ALIASES = {
    "timestamp": "timestamp",
    "time": "timestamp",
    "datetime": "timestamp",
    "date": "timestamp",
    "open_time": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "adj close": "close",
    "adj_close": "close",
    "volume": "volume",
}


def _time_delta(timeframe: str) -> pd.Timedelta:
    try:
        delta = pd.Timedelta(timeframe)
    except ValueError as exc:
        raise DataNormalizationError(f"unsupported timeframe: {timeframe}") from exc
    if delta <= pd.Timedelta(0):
        raise DataNormalizationError("timeframe must be positive")
    return delta


def _canonical_columns(frame: pd.DataFrame) -> pd.DataFrame:
    renamed: dict[str, str] = {}
    for column in frame.columns:
        normalized = str(column).strip().lower()
        canonical = _COLUMN_ALIASES.get(normalized)
        if canonical is not None:
            renamed[column] = canonical
    result = frame.rename(columns=renamed).copy()
    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required.difference(result.columns))
    if missing:
        raise DataNormalizationError(f"missing OHLC columns: {', '.join(missing)}")
    if len(set(result.columns)) != len(result.columns):
        raise DataNormalizationError("duplicate canonical columns after normalization")
    return result


def _utc_timestamps(values: pd.Series, timezone: str) -> pd.Series:
    try:
        parsed = pd.to_datetime(values, errors="raise")
        if getattr(parsed.dt, "tz", None) is None:
            parsed = parsed.dt.tz_localize(timezone)
        else:
            parsed = parsed.dt.tz_convert("UTC")
        return parsed.dt.tz_convert("UTC")
    except (TypeError, ValueError, AttributeError) as exc:
        raise DataNormalizationError(f"timestamps cannot be converted using timezone {timezone!r}") from exc


def normalize_ohlc_frame(
    frame: pd.DataFrame,
    metadata: InstrumentMetadata,
    timeframe: str,
) -> BarSeries:
    """Normalize an OHLC frame while preserving input order for validation."""

    if not isinstance(frame, pd.DataFrame):
        raise DataNormalizationError("input must be a pandas DataFrame")
    delta = _time_delta(timeframe)
    source = _canonical_columns(frame)
    normalized = pd.DataFrame(index=source.index)
    normalized["open_time"] = _utc_timestamps(source["timestamp"], metadata.source_timezone)
    for column in ("open", "high", "low", "close"):
        normalized[column] = pd.to_numeric(source[column], errors="coerce")
    if "volume" in source:
        normalized["volume"] = pd.to_numeric(source["volume"], errors="coerce")
    normalized["close_time"] = normalized["open_time"] + delta
    ordered_columns = ["open_time", "close_time", "open", "high", "low", "close"]
    if "volume" in normalized:
        ordered_columns.append("volume")
    normalized = normalized[ordered_columns].reset_index(drop=True)
    return BarSeries(
        bars=normalized,
        timeframe=timeframe,
        metadata=replace(metadata, source_interval=timeframe),
    )

