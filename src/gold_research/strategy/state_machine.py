"""Finite-state setup contracts for point-3 pullback recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..domain import Direction


class SetupState(str, Enum):
    INITIAL_BREAKOUT = "initial_breakout"
    WAITING_PULLBACK = "waiting_pullback"
    WAITING_REBREAKOUT = "waiting_rebreakout"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"


@dataclass
class Setup:
    setup_id: str
    side: Direction
    state: SetupState
    start_index: int
    breakout_index: int
    breakout_level: float
    breakout_atr: float
    extreme: float
    pullback_extreme: float | None = None
    pullback_bars: int = 0
    age_bars: int = 0
    cancel_reason: str = ""
    transition_log: list[dict[str, object]] = field(default_factory=list)

    def transition(self, state: SetupState, index: int, reason: str) -> None:
        self.transition_log.append(
            {
                "index": index,
                "from": self.state.value,
                "to": state.value,
                "reason": reason,
            }
        )
        self.state = state

    def cancel(self, index: int, reason: str) -> None:
        self.cancel_reason = reason
        self.transition(SetupState.CANCELLED, index, reason)

