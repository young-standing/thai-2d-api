"""Configurable 2D strategies without an assumed calculation rule."""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from decimal import Decimal, localcontext
from typing import Mapping, TypedDict

NORMALIZED_DECIMAL = re.compile(r"^\d+(?:\.\d+)?$")
NORMALIZED_INTEGER = re.compile(r"^\d+$")
SET_INDEX_DECIMAL = re.compile(r"^\d+\.\d+$")
MILLION = Decimal("1000000")


class TwoDCalculationError(ValueError):
    """Raised when strategy configuration or input is invalid."""


class TwoDResult(TypedDict):
    number: str
    index_digit: str
    value_digit: str
    strategy: str


class MyanmarTwoDResult(TypedDict):
    number: str
    index_digit: str
    value_digit: str
    set_index: str
    value_raw: str
    value_million: str
    strategy: str


class TwoDStrategy(ABC):
    """Interface for an explicitly configured 2D calculation strategy."""

    @abstractmethod
    def calculate(self, *, last: str, value: str) -> Mapping[str, str]:
        """Return a 2D result from normalized string inputs."""


def _validate_position(position: object, name: str) -> int:
    if isinstance(position, bool) or not isinstance(position, int):
        raise TwoDCalculationError(f"{name} must be a positive integer")
    if position <= 0:
        raise TwoDCalculationError(f"{name} must be a positive integer")
    return position


def _position_from_environment(name: str) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise TwoDCalculationError(f"{name} is required")
    if not raw.isascii() or not raw.isdigit():
        raise TwoDCalculationError(f"{name} must be a positive integer")
    return _validate_position(int(raw), name)


class ExplicitDigitStrategy(TwoDStrategy):
    """Select explicitly configured right-counted digits; no rule is implied."""

    strategy_name = "explicit_digit"

    def __init__(self, index_digit_position: int, value_digit_position: int):
        self.index_digit_position = _validate_position(
            index_digit_position, "INDEX_DIGIT_POSITION"
        )
        self.value_digit_position = _validate_position(
            value_digit_position, "VALUE_DIGIT_POSITION"
        )

    @classmethod
    def from_environment(cls) -> "ExplicitDigitStrategy":
        return cls(
            index_digit_position=_position_from_environment("INDEX_DIGIT_POSITION"),
            value_digit_position=_position_from_environment("VALUE_DIGIT_POSITION"),
        )

    def calculate(self, *, last: str, value: str) -> TwoDResult:
        if not isinstance(last, str):
            raise TwoDCalculationError("last must be a string")
        if not isinstance(value, str):
            raise TwoDCalculationError("value must be a string")
        if not last:
            raise TwoDCalculationError("last must not be empty")
        if not value:
            raise TwoDCalculationError("value must not be empty")
        if NORMALIZED_DECIMAL.fullmatch(last) is None:
            raise TwoDCalculationError("last must be a normalized decimal string")
        if NORMALIZED_INTEGER.fullmatch(value) is None:
            raise TwoDCalculationError("value must be a normalized integer string")

        index_digits = last.replace(".", "", 1)
        if self.index_digit_position > len(index_digits):
            raise TwoDCalculationError(
                "INDEX_DIGIT_POSITION exceeds the available digits in last"
            )
        if self.value_digit_position > len(value):
            raise TwoDCalculationError(
                "VALUE_DIGIT_POSITION exceeds the available digits in value"
            )

        index_digit = index_digits[-self.index_digit_position]
        value_digit = value[-self.value_digit_position]
        return {
            "number": index_digit + value_digit,
            "index_digit": index_digit,
            "value_digit": value_digit,
            "strategy": self.strategy_name,
        }


class MyanmarTwoDStrategy(TwoDStrategy):
    """Verified displayed SET hundredths + raw-value million-units rule."""

    strategy_name = "set_hundredths_plus_value_million_units"

    def calculate(self, *, last: str, value: str) -> MyanmarTwoDResult:
        if not isinstance(last, str):
            raise TwoDCalculationError("last must be a string")
        if not isinstance(value, str):
            raise TwoDCalculationError("value must be a string")
        if "e" in last.lower() or "e" in value.lower():
            raise TwoDCalculationError("scientific notation is not allowed")
        if SET_INDEX_DECIMAL.fullmatch(last) is None:
            raise TwoDCalculationError(
                "last must contain a decimal point and at least one fractional digit"
            )
        if NORMALIZED_INTEGER.fullmatch(value) is None:
            raise TwoDCalculationError("value must be a non-negative integer string")

        # SET may return additional trailing precision (for example
        # 1621.550000), while the displayed index has exactly two decimals.
        # Decimal formatting produces that display form without using float.
        with localcontext() as context:
            context.prec = max(28, len(last.replace(".", "")) + 2, len(value) + 6)
            displayed_index = format(Decimal(last), ".2f")
            value_million_decimal = Decimal(value) / MILLION
            value_million = format(value_million_decimal, ".6f")
        index_digit = displayed_index.rsplit(".", 1)[1][1]
        integer_million = value_million.partition(".")[0]
        value_digit = integer_million[-1]

        return {
            "number": index_digit + value_digit,
            "index_digit": index_digit,
            "value_digit": value_digit,
            "set_index": last,
            "value_raw": value,
            "value_million": value_million,
            "strategy": self.strategy_name,
        }
