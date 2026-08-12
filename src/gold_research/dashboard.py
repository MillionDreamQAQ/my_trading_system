"""Local Lightweight Charts dashboard for inspecting historical research runs."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .backtest.execution import run_backtest
from .backtest.metrics import summarize_trades
from .config import ResearchConfig
from .data.loader import DataSourceError
from .data.resample import resample_bars
from .data.validate import DataValidationError, validate_bar_series
from .domain import BarSeries, Signal
from .strategy.entry_point_2 import detect_entry_point_2
from .strategy.entry_point_3 import detect_entry_point_3
from .strategy.indicators import add_indicators, trend_state
from .strategy.timeframe_context import build_timeframe_context

STATIC_DIRECTORY = Path(__file__).with_name("dashboard_static")
SUPPORTED_STRATEGIES = ("entry_point_2", "entry_point_3")
# The slowest configured indicator is calculated on 30-minute bars. Seven
# calendar days provides enough completed bars for its initial state, including
# the normal OANDA maintenance and weekend closures.
DASHBOARD_WARMUP = pd.Timedelta(days=7)

DashboardLoader = Callable[[datetime, datetime], BarSeries]


def dashboard_error_message(error: Exception) -> str:
    """Translate expected local-dashboard failures into actionable Chinese text."""

    message = str(error)
    if "OANDA_API_TOKEN is not set" in message:
        return "无法按需加载新日期：当前服务进程未读取到 OANDA_API_TOKEN。请重启看板服务后重试。"
    if "OANDA_AUTH_FAILED" in message:
        return "无法按需加载新日期：OANDA API Token 无效或已失效。"
    if "OANDA_RATE_LIMITED" in message:
        return "OANDA 请求过于频繁，请稍后再试。"
    if "OANDA returned no complete candles" in message:
        return "所选区间没有完整 K 线，可能处于休市时段或结束时间包含当前未完成 K 线。"
    return message


class _CachedWindow:
    """One requested OANDA interval held in the dashboard's in-memory cache."""

    def __init__(self, start: pd.Timestamp, end: pd.Timestamp, series: BarSeries) -> None:
        self.start = start
        self.end = end
        self.series = series


class DashboardDataStore:
    """Fetch and retain only the OANDA windows needed by dashboard requests."""

    def __init__(
        self,
        base: BarSeries,
        config: ResearchConfig,
        loader: DashboardLoader,
        *,
        initial_start: datetime | None = None,
        initial_end: datetime | None = None,
        warmup: pd.Timedelta = DASHBOARD_WARMUP,
    ) -> None:
        if base.start is None or base.end is None:
            raise ValueError("dashboard data is empty")
        if warmup <= pd.Timedelta(0):
            raise ValueError("dashboard warm-up must be positive")
        start = _as_utc(initial_start or base.start)
        end = _as_utc(initial_end or base.end)
        if end <= start:
            raise ValueError("initial dashboard data window must be positive")
        self._config = config
        self._loader = loader
        self._warmup = warmup
        self._windows = [_CachedWindow(start, end, base.copy())]
        self._lock = RLock()

    @staticmethod
    def _missing_windows(
        start: pd.Timestamp,
        end: pd.Timestamp,
        windows: list[_CachedWindow],
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Find uncovered request intervals using logical request boundaries."""

        missing: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        covered_until = start
        for window in sorted(windows, key=lambda item: item.start):
            if window.end <= covered_until:
                continue
            if window.start > covered_until:
                missing.append((covered_until, min(window.start, end)))
            covered_until = max(covered_until, window.end)
            if covered_until >= end:
                break
        if covered_until < end:
            missing.append((covered_until, end))
        return [(left, right) for left, right in missing if right > left]

    def _load_missing(self, start: pd.Timestamp, end: pd.Timestamp) -> None:
        for missing_start, missing_end in self._missing_windows(start, end, self._windows):
            loaded = self._loader(missing_start.to_pydatetime(), missing_end.to_pydatetime())
            if not isinstance(loaded, BarSeries):
                raise TypeError("dashboard data loader must return a BarSeries")
            if loaded.bars.empty:
                raise DataSourceError("OANDA returned no complete candles in the requested range")
            if loaded.timeframe != self._config.timeframes.base:
                raise ValueError("dashboard data loader returned an unexpected timeframe")
            self._windows.append(_CachedWindow(missing_start, missing_end, loaded.copy()))

    def _series_for(self, start: pd.Timestamp, end: pd.Timestamp) -> BarSeries:
        frames = [
            window.series.bars
            for window in self._windows
            if window.start < end and window.end > start
        ]
        if not frames:
            raise ValueError("dashboard data is empty")
        frame = pd.concat(frames, ignore_index=True)
        frame = frame[(frame["open_time"] >= start) & (frame["close_time"] <= end)].copy()
        frame = frame.sort_values("open_time", kind="stable").reset_index(drop=True)
        if frame.empty:
            raise DataSourceError("OANDA returned no complete candles in the requested range")

        series = BarSeries(
            bars=frame,
            timeframe=self._config.timeframes.base,
            metadata=self._windows[0].series.metadata,
        )
        issues = validate_bar_series(
            series,
            expected_interval=self._config.timeframes.base,
            raise_on_error=False,
        )
        if issues:
            raise DataValidationError(issues)
        series.quality_issues.extend(issues)
        return series

    def payload_for(
        self,
        display_start: datetime,
        display_end: datetime,
        *,
        config: ResearchConfig | None = None,
    ) -> dict[str, Any]:
        """Return a fresh payload, downloading only uncached OANDA intervals."""

        start = _as_utc(display_start)
        end = _as_utc(display_end)
        if end <= start:
            raise ValueError("dashboard end must be after dashboard start")
        calculation_start = start - self._warmup
        with self._lock:
            self._load_missing(calculation_start, end)
            base = self._series_for(calculation_start, end)
        return build_dashboard_payload(base, config or self._config, display_start=start, display_end=end)


def _as_utc(value: datetime) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("dashboard dates must be timezone-aware")
    return timestamp.tz_convert("UTC")


def _dashboard_config_for_position(
    config: ResearchConfig,
    margin_per_trade: str | None,
    leverage: str | None,
) -> ResearchConfig:
    """Apply optional dashboard-only sizing values without changing the TOML."""

    if margin_per_trade is None and leverage is None:
        return config

    def positive_number(value: str | None, fallback: float | None, name: str) -> float:
        candidate = fallback if value is None else float(value)
        if candidate is None or not math.isfinite(candidate) or candidate <= 0:
            raise ValueError(f"{name} must be a positive number")
        return candidate

    margin = positive_number(margin_per_trade, config.position.margin_per_trade, "margin_per_trade")
    active_leverage = positive_number(leverage, config.position.leverage, "leverage")
    return replace(
        config,
        position=replace(
            config.position,
            lots=None,
            margin_per_trade=margin,
            leverage=active_leverage,
        ),
    )


def _target_series(series: BarSeries, start: pd.Timestamp, end: pd.Timestamp) -> BarSeries:
    bars = series.bars
    target = bars[(bars["open_time"] >= start) & (bars["close_time"] <= end)].copy()
    return BarSeries(
        bars=target,
        timeframe=series.timeframe,
        metadata=series.metadata,
        quality_issues=list(series.quality_issues),
    )


def _unix_time(value: object) -> int:
    return int(pd.Timestamp(value).timestamp())


def _candles(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "time": _unix_time(row.open_time),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "ema_fast": None if pd.isna(row.ema_fast) else float(row.ema_fast),
            "ema_slow": None if pd.isna(row.ema_slow) else float(row.ema_slow),
            "trend": str(row.trend),
        }
        for row in frame.itertuples(index=False)
    ]


def _signal_record(signal: Signal) -> dict[str, Any]:
    return {
        "id": f"signal:{signal.strategy_id}:{_unix_time(signal.signal_time)}:{signal.side.value}",
        "strategy_id": signal.strategy_id,
        "side": signal.side.value,
        "signal_time": _unix_time(signal.signal_time),
        "entry_time": _unix_time(signal.entry_time) if signal.entry_time is not None else None,
        "breakout_level": signal.breakout_level,
        "atr": signal.atr,
        "reason": signal.reason,
        "base_trend": signal.base_trend,
        "medium_trend": signal.medium_trend,
        "large_trend": signal.large_trend,
        "medium_source_close_time": (
            _unix_time(signal.medium_source_close_time) if signal.medium_source_close_time is not None else None
        ),
        "large_source_close_time": (
            _unix_time(signal.large_source_close_time) if signal.large_source_close_time is not None else None
        ),
        "setup_id": signal.setup_id,
    }


def _trade_record(trade: Any) -> dict[str, Any]:
    return {
        "id": f"trade:{trade.strategy_id}:{_unix_time(trade.signal_time)}:{trade.side.value}",
        "strategy_id": trade.strategy_id,
        "side": trade.side.value,
        "signal_time": _unix_time(trade.signal_time),
        "entry_time": _unix_time(trade.entry_time),
        "exit_time": _unix_time(trade.exit_time),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "lots": trade.lots,
        "quantity": trade.quantity,
        "notional_value": trade.notional_value,
        "required_margin": trade.required_margin,
        "stop_price": trade.stop_price,
        "target_price": trade.target_price,
        "exit_reason": trade.exit_reason,
        "net_pnl": trade.net_pnl,
        "hold_bars": trade.hold_bars,
    }


def _floor_to_timeframe(timestamp: int, timeframe: str) -> int:
    seconds = int(pd.Timedelta(timeframe).total_seconds())
    return timestamp - timestamp % seconds


def _chart_signal_record(signal: Signal, timeframe: str) -> dict[str, Any]:
    record = _signal_record(signal)
    record["chart_time"] = _floor_to_timeframe(record["signal_time"], timeframe)
    return record


def _chart_trade_record(trade: Any, timeframe: str) -> dict[str, Any]:
    record = _trade_record(trade)
    record["chart_entry_time"] = _floor_to_timeframe(record["entry_time"], timeframe)
    record["chart_exit_time"] = _floor_to_timeframe(record["exit_time"], timeframe)
    return record


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _trade_group_analysis(trades: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    values = list(trades)
    net_values = [float(trade.net_pnl) for trade in values]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(values),
        "win_count": len(wins),
        "loss_count": len(losses),
        "net_pnl": sum(net_values),
        "win_rate": _ratio(len(wins), len(values)),
        "average_pnl": _ratio(sum(net_values), len(values)),
        "average_win": _ratio(gross_profit, len(wins)),
        "average_loss": _ratio(gross_loss, len(losses)),
        "profit_factor": _ratio(gross_profit, gross_loss),
        "payoff_ratio": _ratio(
            _ratio(gross_profit, len(wins)) or 0.0,
            _ratio(gross_loss, len(losses)) or 0.0,
        ),
    }


def build_backtest_analysis(
    trades: list[Any] | tuple[Any, ...],
    *,
    signal_count: int,
    unfilled_signal_count: int,
) -> dict[str, Any]:
    """Build presentation-ready analytics without changing backtest execution."""

    ordered = sorted(trades, key=lambda trade: pd.Timestamp(trade.exit_time))
    overall = _trade_group_analysis(ordered)
    net_values = [float(trade.net_pnl) for trade in ordered]
    largest_win = max((value for value in net_values if value > 0), default=None)
    largest_loss = min((value for value in net_values if value <= 0), default=None)
    longest_win_streak = 0
    longest_loss_streak = 0
    current_win_streak = 0
    current_loss_streak = 0
    equity = 0.0
    equity_curve: list[dict[str, float | int]] = []
    daily_pnl: dict[str, dict[str, float | int]] = {}
    by_side: dict[str, list[Any]] = {"long": [], "short": []}
    by_exit: dict[str, list[Any]] = {}

    for trade in ordered:
        value = float(trade.net_pnl)
        if value > 0:
            current_win_streak += 1
            current_loss_streak = 0
        else:
            current_loss_streak += 1
            current_win_streak = 0
        longest_win_streak = max(longest_win_streak, current_win_streak)
        longest_loss_streak = max(longest_loss_streak, current_loss_streak)
        equity += value
        exit_time = pd.Timestamp(trade.exit_time)
        equity_curve.append({"time": _unix_time(exit_time), "value": equity})
        daily_key = exit_time.strftime("%Y-%m-%d")
        daily_summary = daily_pnl.setdefault(daily_key, {"net_pnl": 0.0, "trade_count": 0})
        daily_summary["net_pnl"] = float(daily_summary["net_pnl"]) + value
        daily_summary["trade_count"] = int(daily_summary["trade_count"]) + 1
        by_side[trade.side.value].append(trade)
        by_exit.setdefault(str(trade.exit_reason), []).append(trade)

    overall.update(
        {
            "signal_count": signal_count,
            "unfilled_signal_count": unfilled_signal_count,
            "fill_rate": _ratio(overall["trade_count"], signal_count),
            "average_hold_bars": _ratio(sum(trade.hold_bars for trade in ordered), len(ordered)),
            "largest_win": largest_win,
            "largest_loss": largest_loss,
            "max_notional_value": max((float(getattr(trade, "notional_value", 0.0)) for trade in ordered), default=0.0),
            "max_required_margin": max((float(getattr(trade, "required_margin", 0.0)) for trade in ordered), default=0.0),
            "max_consecutive_wins": longest_win_streak,
            "max_consecutive_losses": longest_loss_streak,
            "equity_curve": equity_curve,
            "daily_pnl": [
                {"date": date, **summary}
                for date, summary in sorted(daily_pnl.items(), reverse=True)
            ],
            "by_side": {side: _trade_group_analysis(group) for side, group in by_side.items()},
            "by_exit_reason": {
                reason: _trade_group_analysis(group) for reason, group in sorted(by_exit.items())
            },
        }
    )
    return overall


def _annotated_frame(series: BarSeries, config: ResearchConfig) -> pd.DataFrame:
    frame = add_indicators(
        series.bars,
        ema_fast=config.trend.ema_fast,
        ema_slow=config.trend.ema_slow,
        slope_lookback=config.trend.slope_lookback,
        atr_period=config.risk.atr_period if series.timeframe == config.timeframes.base else None,
    )
    frame["trend"] = trend_state(frame)
    return frame


def _target_frame(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame[(frame["open_time"] >= start) & (frame["close_time"] <= end)].copy()


def build_dashboard_payload(
    base: BarSeries,
    config: ResearchConfig,
    *,
    display_start: datetime,
    display_end: datetime,
) -> dict[str, Any]:
    """Calculate chart data from a warm-up series and an evaluation window."""

    start = _as_utc(display_start)
    end = _as_utc(display_end)
    if end <= start:
        raise ValueError("dashboard end must be after dashboard start")
    if base.start is None or base.end is None:
        raise ValueError("dashboard data is empty")
    if start < base.start:
        raise ValueError("dashboard range must stay within the loaded data window")

    medium = resample_bars(base, config.timeframes.medium)
    large = resample_bars(base, config.timeframes.large)
    context = build_timeframe_context(
        base,
        medium,
        large,
        config.trend,
        atr_period=config.risk.atr_period,
    )
    target_base = _target_series(base, start, end)
    base_frame = _target_frame(_annotated_frame(base, config), start, end)
    medium_frame = _target_frame(_annotated_frame(medium, config), start, end)
    large_frame = _target_frame(_annotated_frame(large, config), start, end)
    strategies: dict[str, Any] = {}
    for strategy_id in SUPPORTED_STRATEGIES:
        if strategy_id == "entry_point_2":
            detected = detect_entry_point_2(context, config)
        else:
            detected = list(detect_entry_point_3(context, config).signals)
        signals = [signal for signal in detected if start <= signal.signal_time < end]
        backtest = run_backtest(target_base, signals, config)
        metrics = summarize_trades(backtest.trades)
        metrics["signal_count"] = len(signals)
        metrics["unfilled_signal_count"] = len(backtest.unfilled_signals)
        strategies[strategy_id] = {
            "signals": {
                config.timeframes.base: [_chart_signal_record(signal, config.timeframes.base) for signal in signals],
                config.timeframes.medium: [_chart_signal_record(signal, config.timeframes.medium) for signal in signals],
                config.timeframes.large: [_chart_signal_record(signal, config.timeframes.large) for signal in signals],
            },
            "trades": {
                config.timeframes.base: [_chart_trade_record(trade, config.timeframes.base) for trade in backtest.trades],
                config.timeframes.medium: [_chart_trade_record(trade, config.timeframes.medium) for trade in backtest.trades],
                config.timeframes.large: [_chart_trade_record(trade, config.timeframes.large) for trade in backtest.trades],
            },
            "metrics": metrics,
            "analysis": build_backtest_analysis(
                backtest.trades,
                signal_count=len(signals),
                unfilled_signal_count=len(backtest.unfilled_signals),
            ),
            "unfilled": list(backtest.unfilled_signals),
        }

    return {
        "metadata": {
            "symbol": base.metadata.symbol,
            "provider": base.metadata.provider,
            "price_basis": base.metadata.price_basis.value,
            "display_start": start.isoformat(),
            "display_end": end.isoformat(),
            "available_start": base.start.isoformat(),
            "available_end": base.end.isoformat(),
            "warmup_start": base.start.isoformat() if base.start is not None else None,
            "warmup_end": base.end.isoformat() if base.end is not None else None,
            "timeframes": {
                "base": config.timeframes.base,
                "medium": config.timeframes.medium,
                "large": config.timeframes.large,
            },
            "trend": {
                "ema_fast": config.trend.ema_fast,
                "ema_slow": config.trend.ema_slow,
                "slope_lookback": config.trend.slope_lookback,
            },
            "risk": config.to_dict()["risk"],
            "costs": config.to_dict()["costs"],
            "position": config.to_dict()["position"],
        },
        "quality_issues": [issue.to_dict() for issue in base.quality_issues],
        "series": {
            config.timeframes.base: _candles(base_frame),
            config.timeframes.medium: _candles(medium_frame),
            config.timeframes.large: _candles(large_frame),
        },
        "strategies": strategies,
    }


class _DashboardHandler(SimpleHTTPRequestHandler):
    server: "DashboardServer"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIRECTORY), **kwargs)

    def _send_json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _requested_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
        start = query.get("start", [None])[0]
        end = query.get("end", [None])[0]
        margin_per_trade = query.get("margin_per_trade", [None])[0]
        leverage = query.get("leverage", [None])[0]
        active_config = _dashboard_config_for_position(self.server.config, margin_per_trade, leverage)
        if start is None and end is None and margin_per_trade is None and leverage is None:
            return self.server.payload
        if start is None:
            start = self.server.default_display_start.isoformat()
        if end is None:
            end = self.server.default_display_end.isoformat()
        if not start or not end:
            raise ValueError("both start and end are required")
        if self.server.data_store is not None:
            return self.server.data_store.payload_for(
                _as_utc(pd.Timestamp(start)),
                _as_utc(pd.Timestamp(end)),
                config=active_config,
            )
        if self.server.base is None:
            raise ValueError("dashboard range selection is unavailable")
        return build_dashboard_payload(
            self.server.base,
            active_config,
            display_start=_as_utc(pd.Timestamp(start)),
            display_end=_as_utc(pd.Timestamp(end)),
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/dashboard":
            try:
                self._send_json(HTTPStatus.OK, self._requested_payload(parse_qs(parsed.query)))
            except (DataSourceError, DataValidationError, TypeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": dashboard_error_message(exc)})
            return
        if path == "/":
            self.path = "/index.html"
        return super().do_GET()


class DashboardServer(ThreadingHTTPServer):
    payload: dict[str, Any]
    base: BarSeries | None
    config: ResearchConfig | None
    data_store: DashboardDataStore | None
    default_display_start: datetime
    default_display_end: datetime


def serve_dashboard(
    payload: dict[str, Any],
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    base: BarSeries | None = None,
    config: ResearchConfig | None = None,
    data_store: DashboardDataStore | None = None,
    default_display_start: datetime | None = None,
    default_display_end: datetime | None = None,
) -> None:
    """Serve a dashboard until the calling process is stopped."""

    if not STATIC_DIRECTORY.is_dir():
        raise RuntimeError(f"dashboard static directory is missing: {STATIC_DIRECTORY}")
    server = DashboardServer((host, port), _DashboardHandler)
    server.payload = payload
    server.base = base
    server.config = config
    server.data_store = data_store
    if default_display_start is None:
        default_display_start = _as_utc(pd.Timestamp(payload["metadata"]["display_start"])).to_pydatetime()
    if default_display_end is None:
        default_display_end = _as_utc(pd.Timestamp(payload["metadata"]["display_end"])).to_pydatetime()
    server.default_display_start = default_display_start
    server.default_display_end = default_display_end
    print(f"Dashboard available at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
