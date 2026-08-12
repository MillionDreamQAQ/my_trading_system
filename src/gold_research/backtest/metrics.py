"""Aggregate metrics from deterministic trade records."""

from __future__ import annotations

from typing import Any

from .execution import Trade


def summarize_trades(trades: list[Trade] | tuple[Trade, ...]) -> dict[str, Any]:
    values = list(trades)
    net_values = [trade.net_pnl for trade in values]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in net_values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trade_count": len(values),
        "gross_pnl": sum(trade.gross_pnl for trade in values),
        "spread_cost": sum(trade.spread_cost for trade in values),
        "slippage_cost": sum(trade.slippage_cost for trade in values),
        "commission": sum(trade.commission for trade in values),
        "net_pnl": sum(net_values),
        "max_notional_value": max((trade.notional_value for trade in values), default=0.0),
        "max_required_margin": max((trade.required_margin for trade in values), default=0.0),
        "win_count": sum(value > 0 for value in net_values),
        "loss_count": sum(value <= 0 for value in net_values),
        "max_drawdown": max_drawdown,
        "average_mfe": sum(trade.mfe for trade in values) / len(values) if values else 0.0,
        "average_mae": sum(trade.mae for trade in values) / len(values) if values else 0.0,
    }

