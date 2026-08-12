"""Stable domain contracts shared by data, strategy, and backtest modules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import pandas as pd


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


class TrendState(str, Enum):
    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


class PriceBasis(str, Enum):
    MID = "mid"
    BID = "bid"
    ASK = "ask"


@dataclass(frozen=True)
class InstrumentMetadata:
    """Source and instrument facts required to interpret a price series."""

    provider: str
    symbol: str
    price_basis: PriceBasis
    source_timezone: str = "UTC"
    venue: str = ""
    contract_unit: str = ""
    quote_currency: str = "USD"
    tick_size: float | None = None
    point_value: float = 1.0
    source_interval: str = ""
    source_url: str = ""
    acquired_at: str = ""
    raw_start: str = ""
    raw_end: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.price_basis, PriceBasis):
            try:
                object.__setattr__(self, "price_basis", PriceBasis(str(self.price_basis).lower()))
            except ValueError as exc:
                raise ValueError("price_basis must be mid, bid, or ask") from exc
        if not self.provider.strip():
            raise ValueError("provider is required")
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if not self.source_timezone.strip():
            raise ValueError("source_timezone is required")
        if self.point_value <= 0:
            raise ValueError("point_value must be positive")
        if self.tick_size is not None and self.tick_size <= 0:
            raise ValueError("tick_size must be positive when provided")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["price_basis"] = self.price_basis.value
        return result


@dataclass(frozen=True)
class DataQualityIssue:
    code: str
    severity: str
    message: str
    start: str = ""
    end: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BarSeries:
    """OHLC bars with explicit open/close time semantics.

    ``timestamp`` is the bar open time. ``close_time`` is derived as
    ``open_time + timeframe`` and the interval is interpreted as
    ``[open_time, close_time)``.
    """

    bars: pd.DataFrame
    timeframe: str
    metadata: InstrumentMetadata
    quality_issues: list[DataQualityIssue] | None = None

    def __post_init__(self) -> None:
        self.bars = self.bars.copy()
        self.quality_issues = list(self.quality_issues or [])

    @property
    def start(self) -> pd.Timestamp | None:
        if self.bars.empty:
            return None
        return pd.Timestamp(self.bars["open_time"].iloc[0])

    @property
    def end(self) -> pd.Timestamp | None:
        if self.bars.empty:
            return None
        return pd.Timestamp(self.bars["close_time"].iloc[-1])

    def copy(self) -> "BarSeries":
        return BarSeries(
            bars=self.bars.copy(),
            timeframe=self.timeframe,
            metadata=self.metadata,
            quality_issues=list(self.quality_issues),
        )


@dataclass(frozen=True)
class Signal:
    strategy_id: str
    side: Direction
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp | None
    breakout_level: float | None
    atr: float | None
    reason: str
    base_trend: str
    medium_trend: str
    large_trend: str
    medium_source_close_time: pd.Timestamp | None = None
    large_source_close_time: pd.Timestamp | None = None
    setup_id: str = ""

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["side"] = self.side.value
        for key in (
            "signal_time",
            "entry_time",
            "medium_source_close_time",
            "large_source_close_time",
        ):
            value = record[key]
            record[key] = "" if value is None or pd.isna(value) else pd.Timestamp(value).isoformat()
        return record


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    strategy_id: str
    strategy_version: str
    config_fingerprint: str
    data_fingerprint: str
    code_fingerprint: str
    symbol: str
    price_basis: str
    input_start: str
    input_end: str
    timezone: str
    cost_model: dict[str, Any]
    position_model: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[dict[str, Any], ...] = ()
    source_metadata: dict[str, Any] = field(default_factory=dict)
    research_usable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REQUIRED_OHLC_COLUMNS = ("open_time", "close_time", "open", "high", "low", "close")
