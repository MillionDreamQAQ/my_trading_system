"""Deterministic OHLC bar-event simulator with a configurable position limit."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..config import ResearchConfig
from ..domain import BarSeries, Direction, Signal
from .costs import CostModel


@dataclass(frozen=True)
class Trade:
    strategy_id: str
    side: Direction
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    lots: float
    quantity: float
    notional_value: float
    required_margin: float
    stop_price: float
    target_price: float
    exit_reason: str
    gross_pnl: float
    spread_cost: float
    slippage_cost: float
    commission: float
    net_pnl: float
    r_multiple: float
    mfe: float
    mae: float
    hold_bars: int

    def to_record(self) -> dict[str, object]:
        record = self.__dict__.copy()
        record["side"] = self.side.value
        for key in ("signal_time", "entry_time", "exit_time"):
            record[key] = pd.Timestamp(record[key]).isoformat()
        return record


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Trade, ...]
    unfilled_signals: tuple[dict[str, object], ...]


@dataclass
class _Position:
    signal: Signal
    entry_index: int
    entry_time: pd.Timestamp
    entry_reference_price: float
    entry_price: float
    point_value: float
    lots: float
    quantity: float
    leverage: float
    stop_price: float
    target_price: float
    mfe: float = 0.0
    mae: float = 0.0


def _datetime_values(series: pd.Series) -> tuple[object, object | None]:
    values = series.array
    if isinstance(values, pd.arrays.DatetimeArray):
        if values.tz is None:
            return values, None
        return values.asi8, values.tz
    return values, None


def _timestamp_at(values: object, index: int, timezone: object | None) -> pd.Timestamp:
    value = values[index]
    if timezone is not None:
        return pd.Timestamp(int(value), unit="ns", tz=timezone)
    return pd.Timestamp(value)


def _pnl(side: Direction, entry: float, exit: float, quantity: float) -> float:
    return (exit - entry) * quantity if side is Direction.LONG else (entry - exit) * quantity


def _trigger_quote(open_quote: float, level: float, side: Direction, exit_kind: str) -> float:
    """Use the bar open when it has already crossed a stop or target."""

    if side is Direction.LONG and exit_kind == "stop":
        return min(open_quote, level)
    if side is Direction.LONG and exit_kind == "target":
        return max(open_quote, level)
    if side is Direction.SHORT and exit_kind == "stop":
        return max(open_quote, level)
    return min(open_quote, level)


def _trade_from_position(
    position: _Position,
    exit_index: int,
    exit_time: pd.Timestamp,
    exit_price: float,
    exit_reference_price: float,
    reason: str,
    costs: CostModel,
) -> Trade:
    side = position.signal.side
    quantity = position.quantity
    gross = _pnl(side, position.entry_reference_price, exit_reference_price, quantity) * position.point_value
    spread_cost = costs.spread * quantity * position.point_value
    slippage_cost = costs.slippage * 2 * quantity * position.point_value
    commission = costs.round_trip_cost(quantity)
    net = gross - spread_cost - slippage_cost - commission
    risk = abs(position.entry_price - position.stop_price) * quantity * position.point_value
    notional_value = position.entry_price * quantity * position.point_value
    required_margin = notional_value / position.leverage
    return Trade(
        strategy_id=position.signal.strategy_id,
        side=side,
        signal_time=position.signal.signal_time,
        entry_time=position.entry_time,
        exit_time=exit_time,
        entry_price=position.entry_price,
        exit_price=exit_price,
        lots=position.lots,
        quantity=quantity,
        notional_value=notional_value,
        required_margin=required_margin,
        stop_price=position.stop_price,
        target_price=position.target_price,
        exit_reason=reason,
        gross_pnl=gross,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        commission=commission,
        net_pnl=net,
        r_multiple=net / risk if risk else 0.0,
        mfe=position.mfe,
        mae=position.mae,
        hold_bars=exit_index - position.entry_index,
    )


def run_backtest(
    series: BarSeries,
    signals: list[Signal] | tuple[Signal, ...],
    config: ResearchConfig,
) -> BacktestResult:
    """Run signals over bars in chronological order.

    Orders are submitted after the signal close and filled at the next bar's
    open. Candidate signals beyond the configured concurrent-position limit
    are retained as unfilled-by-position records rather than silently deleted.
    """

    bars = series.bars
    costs = CostModel(
        spread=config.costs.spread_value,
        slippage=config.costs.slippage_value,
        commission_per_unit=config.costs.commission_per_unit,
        source_basis=config.instrument.price_basis,
    )
    open_time_index = pd.Index(bars["open_time"])
    entry_indices = np.full(len(signals), -1, dtype=np.intp)
    pending_entries = [
        (signal_index, signal)
        for signal_index, signal in enumerate(signals)
        if signal.entry_time is not None
    ]
    indexed_entries = ()
    if pending_entries:
        entry_times = [pd.Timestamp(signal.entry_time) for _, signal in pending_entries]
        indexed_entries = open_time_index.get_indexer(entry_times)
        for (signal_index, signal), entry_index in zip(pending_entries, indexed_entries):
            entry_indices[signal_index] = entry_index

    by_entry: dict[int, list[Signal]] = {}
    for (_, signal), entry_index in zip(pending_entries, indexed_entries if pending_entries else ()):
        if entry_index >= 0:
            by_entry.setdefault(int(entry_index), []).append(signal)

    open_times, open_time_zone = _datetime_values(bars["open_time"])
    close_times, close_time_zone = _datetime_values(bars["close_time"])
    opens = bars["open"].to_numpy(copy=False)
    highs = bars["high"].to_numpy(copy=False)
    lows = bars["low"].to_numpy(copy=False)
    closes = bars["close"].to_numpy(copy=False)
    positions: list[_Position] = []
    trades: list[Trade] = []
    unfilled: list[dict[str, object]] = []

    for index in range(len(bars)):
        candidates = by_entry.get(index, [])
        if candidates:
            available_slots = max(config.position.max_positions - len(positions), 0)
            for signal in candidates:
                if pd.isna(signal.atr) or signal.atr is None or signal.atr <= 0:
                    unfilled.append({"signal_time": signal.signal_time.isoformat(), "reason": "invalid_atr"})
                    continue
                if available_slots <= 0:
                    unfilled.append({"signal_time": signal.signal_time.isoformat(), "reason": "position_conflict"})
                    continue
                mid_open = float(opens[index])
                entry_price = costs.execution_price(mid_open, signal.side, "entry")
                if signal.side is Direction.LONG:
                    stop = entry_price - config.risk.stop_atr * signal.atr
                    target = entry_price + config.risk.target_atr * signal.atr
                else:
                    stop = entry_price + config.risk.stop_atr * signal.atr
                    target = entry_price - config.risk.target_atr * signal.atr
                quantity = config.position.quantity_for_entry(entry_price, config.instrument.point_value)
                positions.append(_Position(
                    signal,
                    index,
                    _timestamp_at(open_times, index, open_time_zone),
                    mid_open,
                    entry_price,
                    config.instrument.point_value,
                    config.position.lots_for_quantity(quantity),
                    quantity,
                    config.position.leverage,
                    stop,
                    target,
                ))
                available_slots -= 1

        if not positions:
            continue
        open_value = float(opens[index])
        high = float(highs[index])
        low = float(lows[index])
        remaining: list[_Position] = []
        for position in positions:
            side = position.signal.side
            open_quote = costs.quote_price(open_value, side, "exit")
            high_quote = costs.quote_price(high, side, "exit")
            low_quote = costs.quote_price(low, side, "exit")
            exit_reason: str | None = None
            trigger_quote: float | None = None
            if side is Direction.LONG:
                position.mfe = max(position.mfe, high_quote - position.entry_price)
                position.mae = max(position.mae, position.entry_price - low_quote)
                if low_quote <= position.stop_price:
                    exit_reason = "stop"
                    trigger_quote = _trigger_quote(open_quote, position.stop_price, side, exit_reason)
                elif high_quote >= position.target_price:
                    exit_reason = "target"
                    trigger_quote = _trigger_quote(open_quote, position.target_price, side, exit_reason)
            else:
                position.mfe = max(position.mfe, position.entry_price - low_quote)
                position.mae = max(position.mae, high_quote - position.entry_price)
                if high_quote >= position.stop_price:
                    exit_reason = "stop"
                    trigger_quote = _trigger_quote(open_quote, position.stop_price, side, exit_reason)
                elif low_quote <= position.target_price:
                    exit_reason = "target"
                    trigger_quote = _trigger_quote(open_quote, position.target_price, side, exit_reason)

            if exit_reason is not None and trigger_quote is not None:
                exit_reference = costs.reference_price(trigger_quote, side, "exit")
                exit_price = costs.execution_price(exit_reference, side, "exit")
                trades.append(
                    _trade_from_position(
                        position,
                        index,
                        _timestamp_at(open_times, index, open_time_zone),
                        exit_price,
                        exit_reference,
                        exit_reason,
                        costs,
                    )
                )
                continue
            if index - position.entry_index >= config.risk.max_hold_bars:
                close = float(closes[index])
                exit_price = costs.execution_price(close, side, "exit")
                trades.append(
                    _trade_from_position(
                        position,
                        index,
                        _timestamp_at(close_times, index, close_time_zone),
                        exit_price,
                        close,
                        "timeout",
                        costs,
                    )
                )
                continue
            remaining.append(position)
        positions = remaining

    if positions and not bars.empty:
        final_index = len(bars) - 1
        final_close = float(closes[final_index])
        final_close_time = _timestamp_at(close_times, final_index, close_time_zone)
        for position in positions:
            exit_price = costs.execution_price(final_close, position.signal.side, "exit")
            trades.append(
                _trade_from_position(
                    position,
                    final_index,
                    final_close_time,
                    exit_price,
                    final_close,
                    "data_end",
                    costs,
                )
            )
    for signal, entry_index in zip(signals, entry_indices):
        if signal.entry_time is None:
            reason = "no_next_bar"
        elif entry_index < 0:
            reason = "entry_time_not_in_data"
        else:
            # Candidate was either filled or captured as a position conflict.
            continue
        unfilled.append({"signal_time": signal.signal_time.isoformat(), "reason": reason})
    return BacktestResult(tuple(trades), tuple(unfilled))
