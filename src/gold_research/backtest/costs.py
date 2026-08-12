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

    def _quote_adjustment(self, side: Direction, event: str) -> float:
        buy = (side is Direction.LONG and event == "entry") or (side is Direction.SHORT and event == "exit")
        if self.source_basis is PriceBasis.MID:
            return self.spread / 2.0 if buy else -self.spread / 2.0
        if self.source_basis is PriceBasis.BID:
            return self.spread if buy else 0.0
        return 0.0 if buy else -self.spread

    def quote_price(self, source_price: float, side: Direction, event: str) -> float:
        """Return the executable bid/ask quote before adverse slippage.

        Long entry and short exit consume ask; short entry and long exit consume
        bid. ``source_price`` may itself be a mid, bid, or ask series.
        """

        return float(source_price) + self._quote_adjustment(side, event)

    def reference_price(self, quote_price: float, side: Direction, event: str) -> float:
        """Convert an executable bid/ask quote back to the source price basis."""

        return float(quote_price) - self._quote_adjustment(side, event)

    def execution_price(self, source_price: float, side: Direction, event: str) -> float:
        """Return an executable price after bid/ask conversion and slippage."""

        buy = (side is Direction.LONG and event == "entry") or (side is Direction.SHORT and event == "exit")
        adverse_slippage = self.slippage if buy else -self.slippage
        return self.quote_price(source_price, side, event) + adverse_slippage

    def commission(self, quantity: float) -> float:
        return abs(float(quantity)) * self.commission_per_unit

    def round_trip_cost(self, quantity: float) -> float:
        return self.commission(quantity) * 2
