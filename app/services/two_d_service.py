from abc import ABC, abstractmethod

from app.models import MarketSnapshot


class TwoDStrategy(ABC):
    name: str

    @abstractmethod
    def calculate(self, snapshot: MarketSnapshot) -> str | None:
        """Calculate 2D only when an explicitly approved rule is implemented."""


class RawOnlyStrategy(TwoDStrategy):
    """Safe default: exposes raw inputs and deliberately performs no 2D calculation."""

    name = "raw_only"

    def calculate(self, snapshot: MarketSnapshot) -> None:
        return None


def get_two_d_strategy(name: str) -> TwoDStrategy:
    strategies: dict[str, TwoDStrategy] = {"raw_only": RawOnlyStrategy()}
    if name not in strategies:
        raise ValueError(f"Unknown TWO_D_STRATEGY: {name}")
    return strategies[name]
