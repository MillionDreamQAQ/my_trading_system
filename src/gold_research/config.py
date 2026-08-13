"""TOML configuration loading and validation."""

from __future__ import annotations

import hashlib
import json
import math
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
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ConfigError(f"{name} must be finite")
    if result < 0 or (result == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"{name} must be {comparator}")
    return result


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _optional_positive_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    return _positive_float(value, name)


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
class PositionConfig:
    """Position sizing, margin, and concurrency assumptions for each trade."""

    lots: float | None
    margin_per_trade: float | None
    units_per_lot: float
    leverage: float
    max_positions: int

    def quantity_for_entry(self, entry_price: float, point_value: float) -> float:
        """Return units sized from fixed margin, or legacy fixed lots when set."""

        if self.margin_per_trade is not None:
            return self.margin_per_trade * self.leverage / (entry_price * point_value)
        if self.lots is None:
            raise ConfigError("position requires margin_per_trade or lots")
        return self.lots * self.units_per_lot

    def lots_for_quantity(self, quantity: float) -> float:
        return quantity / self.units_per_lot


def _position_config(position: dict[str, Any]) -> PositionConfig:
    """Parse one dashboard/research position model.

    New configurations use fixed margin per trade. The optional legacy lots
    mode remains readable so existing research configurations stay usable.
    """

    if not position:
        return PositionConfig(
            lots=1.0,
            margin_per_trade=None,
            units_per_lot=1.0,
            leverage=1.0,
            max_positions=1,
        )
    margin_per_trade = _optional_positive_float(
        position.get("margin_per_trade"),
        "position.margin_per_trade",
    )
    lots = _optional_positive_float(position.get("lots"), "position.lots")
    if margin_per_trade is None and lots is None:
        raise ConfigError("position requires margin_per_trade or lots")
    if margin_per_trade is not None and lots is not None:
        raise ConfigError("position.margin_per_trade and position.lots cannot both be set")
    return PositionConfig(
        lots=lots,
        margin_per_trade=margin_per_trade,
        units_per_lot=_positive_float(position.get("units_per_lot", 100.0), "position.units_per_lot"),
        leverage=_positive_float(position.get("leverage", 20.0), "position.leverage"),
        max_positions=_positive_int(position.get("max_positions", 1), "position.max_positions"),
    )


@dataclass(frozen=True)
class ResearchConfig:
    instrument: InstrumentConfig
    timeframes: TimeframeConfig
    trend: TrendConfig
    entry_point_2: EntryPoint2Config
    entry_point_3: EntryPoint3Config
    risk: RiskConfig
    costs: CostConfig
    position: PositionConfig
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
        position = raw.get("position", {})
        if not isinstance(instrument, dict):
            raise ConfigError("[instrument] must be a table")
        if not isinstance(timeframes, dict):
            raise ConfigError("[timeframes] must be a table")
        if not isinstance(trend, dict):
            raise ConfigError("[trend] must be a table")
        if not isinstance(entry2, dict) or not isinstance(entry3, dict):
            raise ConfigError("entry point sections must be tables")
        if not isinstance(risk, dict) or not isinstance(costs, dict):
            raise ConfigError("risk and costs must be tables")
        if not isinstance(position, dict):
            raise ConfigError("position must be a table")

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
        if str(costs["spread_model"]).lower() != "fixed":
            raise ConfigError("costs.spread_model must be fixed")
        if str(costs["slippage_model"]).lower() != "fixed":
            raise ConfigError("costs.slippage_model must be fixed")

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
                enabled=_boolean(_required(entry2, "enabled", "entry_point_2"), "entry_point_2.enabled"),
                breakout_lookback=_positive_int(
                    _required(entry2, "breakout_lookback", "entry_point_2"),
                    "entry_point_2.breakout_lookback",
                ),
            ),
            entry_point_3=EntryPoint3Config(
                enabled=_boolean(_required(entry3, "enabled", "entry_point_3"), "entry_point_3.enabled"),
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
                require_explicit_costs=_boolean(costs["require_explicit_costs"], "costs.require_explicit_costs"),
            ),
            position=_position_config(position),
            direction=direction,
            strategy_version=str(raw.get("strategy", {}).get("version", "baseline-v1")),
        )

    def __post_init__(self) -> None:
        if self.risk.target_atr <= self.risk.stop_atr:
            raise ConfigError("risk.target_atr must be greater than risk.stop_atr")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["instrument"]["price_basis"] = self.instrument.price_basis.value
        result["direction"] = self.direction.value
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
    config = ResearchConfig.from_mapping(raw)
    validate_oanda_xauusd_config(config)
    return config


def validate_oanda_xauusd_config(config: ResearchConfig) -> None:
    """Reject user-facing configurations outside the supported OANDA contract."""

    instrument = config.instrument
    if instrument.provider.strip().lower() != "oanda":
        raise ConfigError("instrument.provider must be oanda in this OANDA-only research system")
    if instrument.symbol.strip().upper() != "XAU_USD":
        raise ConfigError("instrument.symbol must be XAU_USD in this OANDA-only research system")
    if instrument.timezone.upper() != "UTC":
        raise ConfigError("instrument.timezone must be UTC for OANDA historical research")
    if instrument.venue != "OANDA spot/CFD":
        raise ConfigError("instrument.venue must be OANDA spot/CFD")
    if instrument.contract_unit != "1 troy ounce":
        raise ConfigError("instrument.contract_unit must be 1 troy ounce")
    if instrument.quote_currency.upper() != "USD":
        raise ConfigError("instrument.quote_currency must be USD")
    if instrument.tick_size != 0.01:
        raise ConfigError("instrument.tick_size must be 0.01")
    if instrument.point_value != 1.0:
        raise ConfigError("instrument.point_value must be 1.0")
