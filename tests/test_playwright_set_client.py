import json
from decimal import Decimal
from pathlib import Path

import pytest

from playwright_set_client import PlaywrightSetClientError, _is_target_response, parse_set_index

SAMPLE_PATH = Path(__file__).parent / "samples" / "set_index_info.json"


def load_sample():
    return json.loads(
        SAMPLE_PATH.read_text(encoding="utf-8"), parse_float=Decimal, parse_int=Decimal
    )


def test_parse_saved_sample_selects_set_and_preserves_decimal_strings():
    result = parse_set_index(load_sample())
    assert result == {
        "last": "1618.20",
        "value": "77145337740",
        "marketDateTime": "2026-07-10T16:45:00+07:00",
        "marketStatus": "Closed",
        "change": "9.90",
        "percentChange": "0.62",
    }


def test_parse_rejects_non_list_index_industry_sectors():
    with pytest.raises(PlaywrightSetClientError, match="indexIndustrySectors"):
        parse_set_index({"indexIndustrySectors": {}})


def test_parse_rejects_missing_set_record():
    with pytest.raises(PlaywrightSetClientError, match="symbol 'SET'"):
        parse_set_index({"indexIndustrySectors": [{"symbol": "SET50"}]})


def test_response_filter_selects_only_successful_index_request():
    class Response:
        url = "https://www.set.or.th/api/set/index/info/list?type=INDEX"
        status = 200

    assert _is_target_response(Response()) is True
    Response.url = "https://www.set.or.th/api/set/index/info/list?type=SECTOR"
    assert _is_target_response(Response()) is False
