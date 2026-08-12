"""Explicit bid/ask, slippage, and commission calculations."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import Direction, PriceBasis


@dataclass(frozen=True)
class CostModel:
    spread: float
    slippage: float
    commission_per_unit: float
    source_basis: PriceBasis = PriceBasis.MID

    def __post_init__(self) -> None:
        if self.spread < 0 or self.slippage < 0 or self.commission_per_unit < 0:
            raise ValueError("cost values cannot be negative")
        if not isinstance(self.source_basis, PriceBasis):
            object.__setattr__(self, "source_basis", PriceBasis(str(self.source_basis)))

    def execution_price(self, mid_price: float, side: Direction, event: str) -> float:
        """Return executable price from a single-price input.

        Long entry and short exit consume ask; short entry and long exit consume
        bid. Fixed slippage is applied against the trader on both paths.
        """

        buy = (side is Direction.LONG and event == "entry") or (side is Direction.SHORT and event == "exit")
        half_spread = self.spread / 2.0
        price = float(mid_price) + (half_spread if buy else -half_spread)
        adverse_slippage = self.slippage if buy else -self.slippage
        return price + adverse_slippage

    def commission(self, quantity: float) -> float:
        return abs(float(quantity)) * self.commission_per_unit

    def round_trip_cost(self, quantity: float) -> float:
        return self.commission(quantity) * 2

