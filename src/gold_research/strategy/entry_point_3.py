"""Causal initial-breakout/pullback/re-breakout state machine."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..config import ResearchConfig
from ..domain import Direction, Signal
from .state_machine import Setup, SetupState


@dataclass(frozen=True)
class EntryPoint3Result:
    signals: tuple[Signal, ...]
    setups: tuple[Setup, ...]


def _setup_id(side: Direction, index: int) -> str:
    return f"{side.value}-{index}"


def _trend_ok(trend: object) -> bool:
    return bool(trend)


def _initial_breakout(
    close: float,
    trend_ok: bool,
    side: Direction,
    level: float,
    previous_close: float | None,
    previous_level: float | None,
) -> bool:
    if not trend_ok or pd.isna(level):
        return False
    if side is Direction.LONG:
        return close > level and (
            previous_close is None or pd.isna(previous_level) or previous_close <= previous_level
        )
    return close < level and (
        previous_close is None or pd.isna(previous_level) or previous_close >= previous_level
    )


def _make_signal(
    row: pd.Series,
    side: Direction,
    setup: Setup,
    next_entry: pd.Timestamp | None,
) -> Signal:
    return Signal(
        strategy_id="entry_point_3",
        side=side,
        signal_time=pd.Timestamp(row["signal_time"]),
        entry_time=next_entry,
        breakout_level=setup.pullback_extreme,
        atr=setup.breakout_atr,
        reason="pullback_rebreakout_above_pullback_high"
        if side is Direction.LONG
        else "pullback_rebreakout_below_pullback_low",
        base_trend=str(row["base_trend"]),
        medium_trend=str(row["medium_trend"]),
        large_trend=str(row["large_trend"]),
        medium_source_close_time=(
            None if pd.isna(row.get("medium_source_close_time")) else pd.Timestamp(row["medium_source_close_time"])
        ),
        large_source_close_time=(
            None if pd.isna(row.get("large_source_close_time")) else pd.Timestamp(row["large_source_close_time"])
        ),
        setup_id=setup.setup_id,
    )


def detect_entry_point_3(context: pd.DataFrame, config: ResearchConfig) -> EntryPoint3Result:
    """Process one base-bar close at a time and retain setup audit logs."""

    if not config.entry_point_3.enabled:
        return EntryPoint3Result((), ())
    frame = context.copy().reset_index(drop=True)
    lookback = config.entry_point_2.breakout_lookback
    frame["long_level"] = frame["high"].shift(1).rolling(lookback, min_periods=lookback).max()
    frame["short_level"] = frame["low"].shift(1).rolling(lookback, min_periods=lookback).min()
    if frame.empty:
        return EntryPoint3Result((), ())
    active: dict[Direction, Setup | None] = {Direction.LONG: None, Direction.SHORT: None}
    completed: list[Setup] = []
    signals: list[Signal] = []
    sequence = 0

    length = len(frame)
    long_allowed = config.direction in {Direction.LONG, Direction.BOTH}
    short_allowed = config.direction in {Direction.SHORT, Direction.BOTH}
    highs = frame["high"].to_numpy(copy=False)
    lows = frame["low"].to_numpy(copy=False)
    closes = frame["close"].to_numpy(copy=False) if "close" in frame else None
    long_levels = frame["long_level"].to_numpy(copy=False)
    short_levels = frame["short_level"].to_numpy(copy=False)
    all_up = frame["all_up"].to_numpy(copy=False) if long_allowed else None
    all_down = frame["all_down"].to_numpy(copy=False) if short_allowed else None
    atrs = frame["atr"].to_numpy(copy=False) if "atr" in frame else None

    for index in range(length):
        high = highs[index]
        low = lows[index]
        close = None
        for side, levels, trends in (
            (Direction.LONG, long_levels, all_up),
            (Direction.SHORT, short_levels, all_down),
        ):
            if trends is None:
                continue
            level = levels[index]
            previous_level = levels[index - 1] if index > 0 else None
            setup = active[side]
            if setup is not None:
                setup.age_bars += 1
                if setup.age_bars > config.entry_point_3.max_setup_bars:
                    setup.cancel(index, "max_setup_bars_exceeded")
                    completed.append(setup)
                    active[side] = None
                    continue
                trend_ok = _trend_ok(trends[index])
                if not trend_ok:
                    setup.cancel(index, "trend_invalidated")
                    completed.append(setup)
                    active[side] = None
                    continue
                close = closes[index] if closes is not None else frame.iloc[index]["close"]
                if side is Direction.LONG:
                    if close < setup.breakout_level:
                        setup.cancel(index, "close_below_initial_breakout")
                        completed.append(setup)
                        active[side] = None
                        continue
                    setup.extreme = max(setup.extreme, float(high))
                    pullback_distance = setup.extreme - float(close)
                    if setup.state is SetupState.INITIAL_BREAKOUT:
                        setup.transition(SetupState.WAITING_PULLBACK, index, "tracking_post_breakout_extreme")
                    if pullback_distance >= config.entry_point_3.pullback_min_atr * setup.breakout_atr:
                        if setup.state is SetupState.WAITING_PULLBACK:
                            setup.transition(SetupState.WAITING_REBREAKOUT, index, "minimum_atr_pullback_reached")
                        setup.pullback_bars += 1
                        setup.pullback_extreme = (
                            float(high)
                            if setup.pullback_extreme is None
                            else max(setup.pullback_extreme, float(high))
                        )
                    if (
                        setup.state is SetupState.WAITING_REBREAKOUT
                        and setup.pullback_bars >= config.entry_point_3.pullback_min_bars
                        and close > float(setup.pullback_extreme)
                    ):
                        setup.transition(SetupState.TRIGGERED, index, "close_rebreakout_above_pullback_high")
                        next_entry = pd.Timestamp(frame.loc[index + 1, "open_time"]) if index + 1 < length else None
                        signals.append(_make_signal(frame.iloc[index], side, setup, next_entry))
                        completed.append(setup)
                        active[side] = None
                        continue
                else:
                    if close > setup.breakout_level:
                        setup.cancel(index, "close_above_initial_breakout")
                        completed.append(setup)
                        active[side] = None
                        continue
                    setup.extreme = min(setup.extreme, float(low))
                    pullback_distance = float(close) - setup.extreme
                    if setup.state is SetupState.INITIAL_BREAKOUT:
                        setup.transition(SetupState.WAITING_PULLBACK, index, "tracking_post_breakout_extreme")
                    if pullback_distance >= config.entry_point_3.pullback_min_atr * setup.breakout_atr:
                        if setup.state is SetupState.WAITING_PULLBACK:
                            setup.transition(SetupState.WAITING_REBREAKOUT, index, "minimum_atr_pullback_reached")
                        setup.pullback_bars += 1
                        setup.pullback_extreme = (
                            float(low)
                            if setup.pullback_extreme is None
                            else min(setup.pullback_extreme, float(low))
                        )
                    if (
                        setup.state is SetupState.WAITING_REBREAKOUT
                        and setup.pullback_bars >= config.entry_point_3.pullback_min_bars
                        and close < float(setup.pullback_extreme)
                    ):
                        setup.transition(SetupState.TRIGGERED, index, "close_rebreakout_below_pullback_low")
                        next_entry = pd.Timestamp(frame.loc[index + 1, "open_time"]) if index + 1 < length else None
                        signals.append(_make_signal(frame.iloc[index], side, setup, next_entry))
                        completed.append(setup)
                        active[side] = None
                        continue

            trend_ok = _trend_ok(trends[index])
            if active[side] is None and trend_ok and pd.notna(level):
                if close is None:
                    close = closes[index] if closes is not None else frame.iloc[index]["close"]
                previous_close = (
                    None
                    if index == 0
                    else closes[index - 1] if closes is not None else frame.iloc[index - 1]["close"]
                )
            else:
                previous_close = None
            if active[side] is None and _initial_breakout(
                close,
                trend_ok,
                side,
                level,
                previous_close,
                previous_level,
            ):
                sequence += 1
                atr = atrs[index] if atrs is not None else None
                if pd.isna(atr) or float(atr) <= 0:
                    continue
                breakout_level = float(level)
                extreme = float(high if side is Direction.LONG else low)
                active[side] = Setup(
                    setup_id=_setup_id(side, sequence),
                    side=side,
                    state=SetupState.INITIAL_BREAKOUT,
                    start_index=index,
                    breakout_index=index,
                    breakout_level=breakout_level,
                    breakout_atr=float(atr),
                    extreme=extreme,
                )
                active[side].transition(SetupState.WAITING_PULLBACK, index, "initial_breakout_confirmed")

    for setup in active.values():
        if setup is not None:
            setup.cancel(length, "data_ended_before_setup_completion")
            completed.append(setup)
    return EntryPoint3Result(tuple(signals), tuple(completed))
