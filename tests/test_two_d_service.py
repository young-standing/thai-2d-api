import pytest

from two_d_service import (
    ExplicitDigitStrategy,
    MyanmarTwoDStrategy,
    TwoDCalculationError,
    TwoDStrategy,
)


def test_strategy_implements_interface_and_calculates_normal_values():
    strategy = ExplicitDigitStrategy(index_digit_position=2, value_digit_position=3)
    assert isinstance(strategy, TwoDStrategy)
    assert strategy.calculate(last="1621.55", value="77145337740") == {
        "number": "57",
        "index_digit": "5",
        "value_digit": "7",
        "strategy": "explicit_digit",
    }


def test_trailing_zeros_are_preserved_for_selection():
    strategy = ExplicitDigitStrategy(index_digit_position=1, value_digit_position=1)
    result = strategy.calculate(last="1621.550000", value="77145337740")
    assert result["index_digit"] == "0"
    assert result["value_digit"] == "0"
    assert result["number"] == "00"


def test_leading_zeros_are_preserved_for_selection():
    strategy = ExplicitDigitStrategy(index_digit_position=5, value_digit_position=6)
    result = strategy.calculate(last="001.20", value="001230")
    assert result == {
        "number": "00",
        "index_digit": "0",
        "value_digit": "0",
        "strategy": "explicit_digit",
    }


@pytest.mark.parametrize("position", [0, -1, 1.5, "1", True, False, None])
def test_invalid_constructor_positions_are_rejected(position):
    with pytest.raises(TwoDCalculationError, match="positive integer"):
        ExplicitDigitStrategy(position, 1)
    with pytest.raises(TwoDCalculationError, match="positive integer"):
        ExplicitDigitStrategy(1, position)


def test_positions_cannot_exceed_available_digits():
    with pytest.raises(TwoDCalculationError, match="INDEX_DIGIT_POSITION exceeds"):
        ExplicitDigitStrategy(5, 1).calculate(last="1.23", value="45")
    with pytest.raises(TwoDCalculationError, match="VALUE_DIGIT_POSITION exceeds"):
        ExplicitDigitStrategy(1, 3).calculate(last="1.23", value="45")


def test_decimal_point_is_removed_before_right_counting():
    strategy = ExplicitDigitStrategy(index_digit_position=3, value_digit_position=1)
    result = strategy.calculate(last="12.34", value="90")
    assert result["index_digit"] == "2"
    assert result["number"] == "20"


@pytest.mark.parametrize("invalid", [12.34, 1234, True, False])
def test_last_rejects_float_int_and_bool(invalid):
    with pytest.raises(TwoDCalculationError, match="last must be a string"):
        ExplicitDigitStrategy(1, 1).calculate(last=invalid, value="10")


@pytest.mark.parametrize("invalid", [12.34, 1234, True, False])
def test_value_rejects_float_int_and_bool(invalid):
    with pytest.raises(TwoDCalculationError, match="value must be a string"):
        ExplicitDigitStrategy(1, 1).calculate(last="1.00", value=invalid)


@pytest.mark.parametrize(
    ("last", "value", "message"),
    [
        ("", "10", "last must not be empty"),
        ("1.00", "", "value must not be empty"),
        (".", "10", "normalized decimal"),
        ("1.2.3", "10", "normalized decimal"),
        ("1.00", "12.3", "normalized integer"),
        ("1.00", "1e3", "normalized integer"),
    ],
)
def test_empty_and_malformed_values_are_rejected(last, value, message):
    with pytest.raises(TwoDCalculationError, match=message):
        ExplicitDigitStrategy(1, 1).calculate(last=last, value=value)


def test_positions_load_from_environment(monkeypatch):
    monkeypatch.setenv("INDEX_DIGIT_POSITION", "2")
    monkeypatch.setenv("VALUE_DIGIT_POSITION", "3")
    strategy = ExplicitDigitStrategy.from_environment()
    assert strategy.index_digit_position == 2
    assert strategy.value_digit_position == 3


@pytest.mark.parametrize("name", ["INDEX_DIGIT_POSITION", "VALUE_DIGIT_POSITION"])
def test_missing_environment_positions_are_rejected(monkeypatch, name):
    monkeypatch.setenv("INDEX_DIGIT_POSITION", "1")
    monkeypatch.setenv("VALUE_DIGIT_POSITION", "1")
    monkeypatch.delenv(name)
    with pytest.raises(TwoDCalculationError, match=f"{name} is required"):
        ExplicitDigitStrategy.from_environment()


@pytest.mark.parametrize(
    ("last", "value", "expected_number", "expected_million"),
    [
        ("1616.54", "33106740000", "46", "33106.740000"),
        ("1617.27", "40944120000", "74", "40944.120000"),
        ("1621.550000", "77145337740", "55", "77145.337740"),
    ],
)
def test_verified_myanmar_two_d_examples(last, value, expected_number, expected_million):
    result = MyanmarTwoDStrategy().calculate(last=last, value=value)
    assert result["number"] == expected_number
    assert result["value_million"] == expected_million
    assert result["set_index"] == last
    assert result["value_raw"] == value


def test_verified_strategy_returns_exact_schema():
    result = MyanmarTwoDStrategy().calculate(last="1621.550000", value="77145337740")
    assert result == {
        "number": "55",
        "index_digit": "5",
        "value_digit": "5",
        "set_index": "1621.550000",
        "value_raw": "77145337740",
        "value_million": "77145.337740",
        "strategy": "set_hundredths_plus_value_million_units",
    }


def test_extra_trailing_zeros_do_not_replace_the_displayed_hundredths_digit():
    result = MyanmarTwoDStrategy().calculate(last="100.1200", value="6000000")
    assert result["index_digit"] == "2"
    assert result["number"] == "26"
    assert result["set_index"] == "100.1200"


def test_leading_zero_result_is_preserved():
    result = MyanmarTwoDStrategy().calculate(last="10.00", value="5000000")
    assert result["number"] == "05"


@pytest.mark.parametrize("invalid", ["1616", "1616.", ".54", "", "1.2.3", "-1.20"])
def test_verified_strategy_rejects_invalid_decimal_index(invalid):
    with pytest.raises(TwoDCalculationError, match="decimal point"):
        MyanmarTwoDStrategy().calculate(last=invalid, value="33106740000")


@pytest.mark.parametrize("field", ["last", "value"])
@pytest.mark.parametrize("invalid", [1.2, 1, True, False])
def test_verified_strategy_rejects_float_int_and_bool(field, invalid):
    inputs = {"last": "1616.54", "value": "33106740000"}
    inputs[field] = invalid
    with pytest.raises(TwoDCalculationError, match=f"{field} must be a string"):
        MyanmarTwoDStrategy().calculate(**inputs)


@pytest.mark.parametrize("invalid", ["-1", "-1000000"])
def test_verified_strategy_rejects_negative_value(invalid):
    with pytest.raises(TwoDCalculationError, match="non-negative integer"):
        MyanmarTwoDStrategy().calculate(last="1616.54", value=invalid)


@pytest.mark.parametrize(
    ("last", "value"),
    [("1.61654E+3", "33106740000"), ("1616.54", "3.310674E+10")],
)
def test_verified_strategy_rejects_scientific_notation(last, value):
    with pytest.raises(TwoDCalculationError, match="scientific notation"):
        MyanmarTwoDStrategy().calculate(last=last, value=value)


def test_value_below_one_million_uses_zero_integer_units_digit():
    result = MyanmarTwoDStrategy().calculate(last="10.09", value="999999")
    assert result["value_million"] == "0.999999"
    assert result["value_digit"] == "0"
    assert result["number"] == "90"
