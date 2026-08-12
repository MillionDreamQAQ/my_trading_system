"""Deterministic one-position-at-a-time OHLC bar-event simulator."""

from __future__ import annotations

from dataclasses import dataclass

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
    quantity: float
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
    entry_price: float
    stop_price: float
    target_price: float
    mfe: float = 0.0
    mae: float = 0.0


def _pnl(side: Direction, entry: float, exit: float, quantity: float) -> float:
    return (exit - entry) * quantity if side is Direction.LONG else (entry - exit) * quantity


def _trade_from_position(
    position: _Position,
    bars: pd.DataFrame,
    exit_index: int,
    exit_price: float,
    reason: str,
    costs: CostModel,
) -> Trade:
    side = position.signal.side
    quantity = 1.0
    gross = _pnl(side, position.entry_price, exit_price, quantity)
    spread_cost = costs.spread * quantity
    slippage_cost = costs.slippage * 2 * quantity
    commission = costs.round_trip_cost(quantity)
    # Entry/exit prices already include bid/ask and slippage. Keep those
    # components separately for attribution, but do not deduct them twice.
    net = gross - commission
    risk = abs(position.entry_price - position.stop_price)
    return Trade(
        strategy_id=position.signal.strategy_id,
        side=side,
        signal_time=position.signal.signal_time,
        entry_time=position.entry_time,
        exit_time=pd.Timestamp(bars.iloc[exit_index]["open_time"]),
        entry_price=position.entry_price,
        exit_price=exit_price,
        quantity=quantity,
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
    open. Existing positions retain candidate signals as unfilled-by-position
    records rather than silently deleting them.
    """

    bars = series.bars.reset_index(drop=True)
    costs = CostModel(
        spread=config.costs.spread_value,
        slippage=config.costs.slippage_value,
        commission_per_unit=config.costs.commission_per_unit,
        source_basis=config.instrument.price_basis,
    )
    by_entry: dict[pd.Timestamp, list[Signal]] = {}
    for signal in signals:
        if signal.entry_time is not None:
            by_entry.setdefault(pd.Timestamp(signal.entry_time), []).append(signal)
    position: _Position | None = None
    trades: list[Trade] = []
    unfilled: list[dict[str, object]] = []

    for index, row in bars.iterrows():
        open_time = pd.Timestamp(row["open_time"])
        if position is None:
            candidates = by_entry.get(open_time, [])
            if candidates:
                signal = candidates[0]
                if pd.isna(signal.atr) or signal.atr is None or signal.atr <= 0:
                    unfilled.append({"signal_time": signal.signal_time.isoformat(), "reason": "invalid_atr"})
                else:
                    mid_open = float(row["open"])
                    entry_price = costs.execution_price(mid_open, signal.side, "entry")
                    if signal.side is Direction.LONG:
                        stop = entry_price - config.risk.stop_atr * signal.atr
                        target = entry_price + config.risk.target_atr * signal.atr
                    else:
                        stop = entry_price + config.risk.stop_atr * signal.atr
                        target = entry_price - config.risk.target_atr * signal.atr
                    position = _Position(signal, index, open_time, entry_price, stop, target)
            for ignored in candidates[1:]:
                unfilled.append({"signal_time": ignored.signal_time.isoformat(), "reason": "position_conflict"})

        if position is None:
            continue
        high = float(row["high"])
        low = float(row["low"])
        side = position.signal.side
        if side is Direction.LONG:
            position.mfe = max(position.mfe, high - position.entry_price)
            position.mae = max(position.mae, position.entry_price - low)
            if low <= position.stop_price:
                trigger = min(float(row["open"]), position.stop_price) if index == position.entry_index else position.stop_price
                exit_price = costs.execution_price(trigger, side, "exit")
                trades.append(_trade_from_position(position, bars, index, exit_price, "stop", costs))
                position = None
            elif high >= position.target_price:
                trigger = max(float(row["open"]), position.target_price) if index == position.entry_index else position.target_price
                exit_price = costs.execution_price(trigger, side, "exit")
                trades.append(_trade_from_position(position, bars, index, exit_price, "target", costs))
                position = None
        else:
            position.mfe = max(position.mfe, position.entry_price - low)
            position.mae = max(position.mae, high - position.entry_price)
            if high >= position.stop_price:
                trigger = max(float(row["open"]), position.stop_price) if index == position.entry_index else position.stop_price
                exit_price = costs.execution_price(trigger, side, "exit")
                trades.append(_trade_from_position(position, bars, index, exit_price, "stop", costs))
                position = None
            elif low <= position.target_price:
                trigger = min(float(row["open"]), position.target_price) if index == position.entry_index else position.target_price
                exit_price = costs.execution_price(trigger, side, "exit")
                trades.append(_trade_from_position(position, bars, index, exit_price, "target", costs))
                position = None
        if position is not None and index - position.entry_index >= config.risk.max_hold_bars:
            exit_price = costs.execution_price(float(row["close"]), side, "exit")
            trades.append(_trade_from_position(position, bars, index, exit_price, "timeout", costs))
            position = None

    for signal in signals:
        if signal.entry_time is None:
            unfilled.append({"signal_time": signal.signal_time.isoformat(), "reason": "no_next_bar"})
        elif pd.Timestamp(signal.entry_time) not in set(bars["open_time"]):
            unfilled.append({"signal_time": signal.signal_time.isoformat(), "reason": "entry_time_not_in_data"})
    return BacktestResult(tuple(trades), tuple(unfilled))
