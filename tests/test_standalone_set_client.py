import pytest

from set_client import SetClientError, parse_set_index


def test_parse_set_index_preserves_last_and_value_as_strings():
    result = parse_set_index(
        {
            "indexIndustrySectors": [
                {"symbol": "SET50", "last": "800.00"},
                {
                    "symbol": "SET",
                    "last": "1234.50",
                    "value": "7.714533774E+10",
                    "marketDateTime": "2026-07-12T10:30:00+07:00",
                    "marketStatus": "Open",
                    "change": "1.20",
                    "percentChange": "0.10",
                },
            ]
        }
    )
    assert result == {
        "last": "1234.50",
        "value": "77145337740",
        "marketDateTime": "2026-07-12T10:30:00+07:00",
        "marketStatus": "Open",
        "change": "1.20",
        "percentChange": "0.10",
    }


def test_all_numeric_fields_are_plain_strings_without_exponents():
    result = parse_set_index(
        {
            "indexIndustrySectors": [
                {
                    "symbol": "SET",
                    "last": "1.2300E+3",
                    "value": "7.714533774E+10",
                    "marketDateTime": "2026-07-12T10:30:00+07:00",
                    "marketStatus": "Open",
                    "change": "-1.2500E-1",
                    "percentChange": "2.500E-2",
                }
            ]
        }
    )
    assert result["last"] == "1230.0"
    assert result["value"] == "77145337740"
    assert result["change"] == "-0.12500"
    assert result["percentChange"] == "0.02500"
    assert all(isinstance(result[field], str) for field in ("last", "value", "change", "percentChange"))


def test_parse_set_index_rejects_missing_array():
    with pytest.raises(SetClientError, match="indexIndustrySectors"):
        parse_set_index({})


def test_parse_set_index_rejects_missing_set_record():
    with pytest.raises(SetClientError, match="symbol 'SET'"):
        parse_set_index({"indexIndustrySectors": [{"symbol": "SET50"}]})
