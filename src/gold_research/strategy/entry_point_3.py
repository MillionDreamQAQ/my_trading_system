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


def _trend_ok(row: pd.Series, side: Direction) -> bool:
    return bool(row["all_up"] if side is Direction.LONG else row["all_down"])


def _initial_breakout(
    frame: pd.DataFrame,
    index: int,
    side: Direction,
    level_column: str,
) -> bool:
    row = frame.iloc[index]
    if not _trend_ok(row, side) or pd.isna(row[level_column]):
        return False
    previous_close = frame.iloc[index - 1]["close"] if index > 0 else None
    previous_level = frame.iloc[index - 1][level_column] if index > 0 else None
    if side is Direction.LONG:
        return row["close"] > row[level_column] and (
            previous_close is None or pd.isna(previous_level) or previous_close <= previous_level
        )
    return row["close"] < row[level_column] and (
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
    active: dict[Direction, Setup | None] = {Direction.LONG: None, Direction.SHORT: None}
    completed: list[Setup] = []
    signals: list[Signal] = []
    sequence = 0

    for index, row in frame.iterrows():
        for side, level_column in (
            (Direction.LONG, "long_level"),
            (Direction.SHORT, "short_level"),
        ):
            if config.direction not in {side, Direction.BOTH}:
                continue
            setup = active[side]
            if setup is not None:
                setup.age_bars += 1
                if setup.age_bars > config.entry_point_3.max_setup_bars:
                    setup.cancel(index, "max_setup_bars_exceeded")
                    completed.append(setup)
                    active[side] = None
                    continue
                if not _trend_ok(row, side):
                    setup.cancel(index, "trend_invalidated")
                    completed.append(setup)
                    active[side] = None
                    continue
                if side is Direction.LONG:
                    if row["close"] < setup.breakout_level:
                        setup.cancel(index, "close_below_initial_breakout")
                        completed.append(setup)
                        active[side] = None
                        continue
                    setup.extreme = max(setup.extreme, float(row["high"]))
                    pullback_distance = setup.extreme - float(row["close"])
                    if setup.state is SetupState.INITIAL_BREAKOUT:
                        setup.transition(SetupState.WAITING_PULLBACK, index, "tracking_post_breakout_extreme")
                    if pullback_distance >= config.entry_point_3.pullback_min_atr * setup.breakout_atr:
                        if setup.state is SetupState.WAITING_PULLBACK:
                            setup.transition(SetupState.WAITING_REBREAKOUT, index, "minimum_atr_pullback_reached")
                        setup.pullback_bars += 1
                        setup.pullback_extreme = (
                            float(row["high"])
                            if setup.pullback_extreme is None
                            else max(setup.pullback_extreme, float(row["high"]))
                        )
                    if (
                        setup.state is SetupState.WAITING_REBREAKOUT
                        and setup.pullback_bars >= config.entry_point_3.pullback_min_bars
                        and row["close"] > float(setup.pullback_extreme)
                    ):
                        setup.transition(SetupState.TRIGGERED, index, "close_rebreakout_above_pullback_high")
                        next_entry = pd.Timestamp(frame.loc[index + 1, "open_time"]) if index + 1 < len(frame) else None
                        signals.append(_make_signal(row, side, setup, next_entry))
                        completed.append(setup)
                        active[side] = None
                        continue
                else:
                    if row["close"] > setup.breakout_level:
                        setup.cancel(index, "close_above_initial_breakout")
                        completed.append(setup)
                        active[side] = None
                        continue
                    setup.extreme = min(setup.extreme, float(row["low"]))
                    pullback_distance = float(row["close"]) - setup.extreme
                    if setup.state is SetupState.INITIAL_BREAKOUT:
                        setup.transition(SetupState.WAITING_PULLBACK, index, "tracking_post_breakout_extreme")
                    if pullback_distance >= config.entry_point_3.pullback_min_atr * setup.breakout_atr:
                        if setup.state is SetupState.WAITING_PULLBACK:
                            setup.transition(SetupState.WAITING_REBREAKOUT, index, "minimum_atr_pullback_reached")
                        setup.pullback_bars += 1
                        setup.pullback_extreme = (
                            float(row["low"])
                            if setup.pullback_extreme is None
                            else min(setup.pullback_extreme, float(row["low"]))
                        )
                    if (
                        setup.state is SetupState.WAITING_REBREAKOUT
                        and setup.pullback_bars >= config.entry_point_3.pullback_min_bars
                        and row["close"] < float(setup.pullback_extreme)
                    ):
                        setup.transition(SetupState.TRIGGERED, index, "close_rebreakout_below_pullback_low")
                        next_entry = pd.Timestamp(frame.loc[index + 1, "open_time"]) if index + 1 < len(frame) else None
                        signals.append(_make_signal(row, side, setup, next_entry))
                        completed.append(setup)
                        active[side] = None
                        continue

            if active[side] is None and _initial_breakout(frame, index, side, level_column):
                sequence += 1
                atr = row.get("atr")
                if pd.isna(atr) or float(atr) <= 0:
                    continue
                level = float(row[level_column])
                extreme = float(row["high"] if side is Direction.LONG else row["low"])
                active[side] = Setup(
                    setup_id=_setup_id(side, sequence),
                    side=side,
                    state=SetupState.INITIAL_BREAKOUT,
                    start_index=index,
                    breakout_index=index,
                    breakout_level=level,
                    breakout_atr=float(atr),
                    extreme=extreme,
                )
                active[side].transition(SetupState.WAITING_PULLBACK, index, "initial_breakout_confirmed")

    for setup in active.values():
        if setup is not None:
            setup.cancel(len(frame), "data_ended_before_setup_completion")
            completed.append(setup)
    return EntryPoint3Result(tuple(signals), tuple(completed))
