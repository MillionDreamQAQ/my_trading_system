"""TOML configuration loading and validation."""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .domain import Direction, PriceBasis


class ConfigError(ValueError):
    """Raised when a research configuration cannot be used safely."""


def _required(mapping: dict[str, Any], key: str, section: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"missing required config field: [{section}] {key}")
    return mapping[key]


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _positive_float(value: Any, name: str, allow_zero: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if result < 0 or (result == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"{name} must be {comparator}")
    return result


@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str
    provider: str
    price_basis: PriceBasis
    timezone: str
    venue: str
    contract_unit: str
    quote_currency: str
    tick_size: float | None
    point_value: float


@dataclass(frozen=True)
class TimeframeConfig:
    base: str
    medium: str
    large: str
    timezone: str


@dataclass(frozen=True)
class TrendConfig:
    ema_fast: int
    ema_slow: int
    slope_lookback: int


@dataclass(frozen=True)
class EntryPoint2Config:
    enabled: bool
    breakout_lookback: int


@dataclass(frozen=True)
class EntryPoint3Config:
    enabled: bool
    pullback_min_atr: float
    pullback_min_bars: int
    max_setup_bars: int


@dataclass(frozen=True)
class RiskConfig:
    atr_period: int
    stop_atr: float
    target_atr: float
    max_hold_bars: int


@dataclass(frozen=True)
class CostConfig:
    spread_model: str
    spread_value: float
    slippage_model: str
    slippage_value: float
    commission_per_unit: float
    require_explicit_costs: bool


@dataclass(frozen=True)
class DataQualityConfig:
    missing_bar_policy: str
    max_gap_bars: int
    closed_weekdays: tuple[int, ...] = ()


@dataclass(frozen=True)
class ResearchConfig:
    instrument: InstrumentConfig
    timeframes: TimeframeConfig
    trend: TrendConfig
    entry_point_2: EntryPoint2Config
    entry_point_3: EntryPoint3Config
    risk: RiskConfig
    costs: CostConfig
    data_quality: DataQualityConfig
    direction: Direction = Direction.BOTH
    strategy_version: str = "baseline-v1"

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ResearchConfig":
        instrument = _required(raw, "instrument", "root")
        timeframes = _required(raw, "timeframes", "root")
        trend = _required(raw, "trend", "root")
        entry2 = _required(raw, "entry_point_2", "root")
        entry3 = _required(raw, "entry_point_3", "root")
        risk = _required(raw, "risk", "root")
        costs = _required(raw, "costs", "root")
        quality = _required(raw, "data_quality", "root")
        if not isinstance(instrument, dict):
            raise ConfigError("[instrument] must be a table")
        if not isinstance(timeframes, dict):
            raise ConfigError("[timeframes] must be a table")
        if not isinstance(trend, dict):
            raise ConfigError("[trend] must be a table")
        if not isinstance(entry2, dict) or not isinstance(entry3, dict):
            raise ConfigError("entry point sections must be tables")
        if not isinstance(risk, dict) or not isinstance(costs, dict) or not isinstance(quality, dict):
            raise ConfigError("risk, costs, and data_quality must be tables")

        symbol = str(_required(instrument, "symbol", "instrument")).strip()
        provider = str(_required(instrument, "provider", "instrument")).strip()
        timezone = str(_required(instrument, "timezone", "instrument")).strip()
        if not symbol or not provider or not timezone:
            raise ConfigError("instrument symbol, provider, and timezone are required")
        try:
            price_basis = PriceBasis(str(_required(instrument, "price_basis", "instrument")).lower())
        except ValueError as exc:
            raise ConfigError("instrument.price_basis must be mid, bid, or ask") from exc

        tf_base = str(_required(timeframes, "base", "timeframes"))
        tf_medium = str(_required(timeframes, "medium", "timeframes"))
        tf_large = str(_required(timeframes, "large", "timeframes"))
        tf_timezone = str(_required(timeframes, "timezone", "timeframes"))
        if tf_timezone != timezone:
            raise ConfigError("instrument.timezone and timeframes.timezone must match")

        trend_config = TrendConfig(
            ema_fast=_positive_int(_required(trend, "ema_fast", "trend"), "trend.ema_fast"),
            ema_slow=_positive_int(_required(trend, "ema_slow", "trend"), "trend.ema_slow"),
            slope_lookback=_positive_int(_required(trend, "slope_lookback", "trend"), "trend.slope_lookback"),
        )
        if trend_config.ema_fast >= trend_config.ema_slow:
            raise ConfigError("trend.ema_fast must be smaller than trend.ema_slow")

        cost_fields = (
            "spread_model",
            "spread_value",
            "slippage_model",
            "slippage_value",
            "commission_per_unit",
            "require_explicit_costs",
        )
        for field in cost_fields:
            _required(costs, field, "costs")

        direction_value = str(raw.get("strategy", {}).get("direction", "both")).lower()
        try:
            direction = Direction(direction_value)
        except ValueError as exc:
            raise ConfigError("strategy.direction must be long, short, or both") from exc

        return cls(
            instrument=InstrumentConfig(
                symbol=symbol,
                provider=provider,
                price_basis=price_basis,
                timezone=timezone,
                venue=str(instrument.get("venue", "")),
                contract_unit=str(instrument.get("contract_unit", "")),
                quote_currency=str(instrument.get("quote_currency", "USD")),
                tick_size=(
                    _positive_float(instrument["tick_size"], "instrument.tick_size")
                    if instrument.get("tick_size") is not None
                    else None
                ),
                point_value=_positive_float(instrument.get("point_value", 1.0), "instrument.point_value"),
            ),
            timeframes=TimeframeConfig(tf_base, tf_medium, tf_large, tf_timezone),
            trend=trend_config,
            entry_point_2=EntryPoint2Config(
                enabled=bool(_required(entry2, "enabled", "entry_point_2")),
                breakout_lookback=_positive_int(
                    _required(entry2, "breakout_lookback", "entry_point_2"),
                    "entry_point_2.breakout_lookback",
                ),
            ),
            entry_point_3=EntryPoint3Config(
                enabled=bool(_required(entry3, "enabled", "entry_point_3")),
                pullback_min_atr=_positive_float(
                    _required(entry3, "pullback_min_atr", "entry_point_3"),
                    "entry_point_3.pullback_min_atr",
                ),
                pullback_min_bars=_positive_int(
                    _required(entry3, "pullback_min_bars", "entry_point_3"),
                    "entry_point_3.pullback_min_bars",
                ),
                max_setup_bars=_positive_int(
                    _required(entry3, "max_setup_bars", "entry_point_3"),
                    "entry_point_3.max_setup_bars",
                ),
            ),
            risk=RiskConfig(
                atr_period=_positive_int(_required(risk, "atr_period", "risk"), "risk.atr_period"),
                stop_atr=_positive_float(_required(risk, "stop_atr", "risk"), "risk.stop_atr"),
                target_atr=_positive_float(_required(risk, "target_atr", "risk"), "risk.target_atr"),
                max_hold_bars=_positive_int(_required(risk, "max_hold_bars", "risk"), "risk.max_hold_bars"),
            ),
            costs=CostConfig(
                spread_model=str(costs["spread_model"]),
                spread_value=_positive_float(costs["spread_value"], "costs.spread_value", allow_zero=True),
                slippage_model=str(costs["slippage_model"]),
                slippage_value=_positive_float(costs["slippage_value"], "costs.slippage_value", allow_zero=True),
                commission_per_unit=_positive_float(
                    costs["commission_per_unit"], "costs.commission_per_unit", allow_zero=True
                ),
                require_explicit_costs=bool(costs["require_explicit_costs"]),
            ),
            data_quality=DataQualityConfig(
                missing_bar_policy=str(_required(quality, "missing_bar_policy", "data_quality")),
                max_gap_bars=int(_required(quality, "max_gap_bars", "data_quality")),
                closed_weekdays=tuple(int(day) for day in quality.get("closed_weekdays", [])),
            ),
            direction=direction,
            strategy_version=str(raw.get("strategy", {}).get("version", "baseline-v1")),
        )

    def __post_init__(self) -> None:
        if self.risk.target_atr <= self.risk.stop_atr:
            raise ConfigError("risk.target_atr must be greater than risk.stop_atr")
        if self.data_quality.missing_bar_policy not in {"block", "warn", "ignore"}:
            raise ConfigError("data_quality.missing_bar_policy must be block, warn, or ignore")
        if self.data_quality.max_gap_bars < 0:
            raise ConfigError("data_quality.max_gap_bars cannot be negative")
        if any(day < 0 or day > 6 for day in self.data_quality.closed_weekdays):
            raise ConfigError("data_quality.closed_weekdays must contain weekday numbers 0..6")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["instrument"]["price_basis"] = self.instrument.price_basis.value
        result["direction"] = self.direction.value
        result["data_quality"]["closed_weekdays"] = list(self.data_quality.closed_weekdays)
        return result

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_config(path: str | Path) -> ResearchConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML config: {config_path}: {exc}") from exc
    return ResearchConfig.from_mapping(raw)

