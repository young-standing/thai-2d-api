import pytest

from three_d_service import ThreeDValidationError, calculate_three_d


@pytest.mark.parametrize(
    ("first", "result"),
    [("751495", "495"), ("100007", "007"), ("000123", "123")],
)
def test_calculation_preserves_digits(first, result):
    assert calculate_three_d(first) == {
        "first_prize": first,
        "three_d": result,
        "strategy": "first_prize_last_three_digits",
    }


@pytest.mark.parametrize(
    "value",
    [751495, 751495.0, True, "", "   ", "12345", "1234567", "-12345", "123.45", "1e0005", "๑๒๓๔๕๖"],
)
def test_rejects_non_exact_ascii_six_digit_values(value):
    with pytest.raises(ThreeDValidationError):
        calculate_three_d(value)
