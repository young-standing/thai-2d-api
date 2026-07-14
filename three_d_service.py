"""Pure Thai Government Lottery first-prize to 3D calculation."""

from __future__ import annotations

import re

STRATEGY = "first_prize_last_three_digits"
_SIX_ASCII_DIGITS = re.compile(r"^[0-9]{6}$", re.ASCII)


class ThreeDValidationError(ValueError):
    """Raised when a first-prize value is not an exact six-digit string."""


def calculate_three_d(first_prize: str) -> dict[str, str]:
    """Return the final three digits without numeric conversion."""
    if not isinstance(first_prize, str):
        raise ThreeDValidationError("first_prize must be a string")
    if _SIX_ASCII_DIGITS.fullmatch(first_prize) is None:
        raise ThreeDValidationError("first_prize must contain exactly six ASCII digits")
    return {
        "first_prize": first_prize,
        "three_d": first_prize[-3:],
        "strategy": STRATEGY,
    }
