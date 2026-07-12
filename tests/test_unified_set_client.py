import json

import pytest
import requests

from set_client import SetClientError
from unified_set_client import UnifiedSetClient, UnifiedSetClientError


def normalized_payload():
    return {
        "last": "1621.550000",
        "value": "77145337740",
        "marketDateTime": "2026-07-11T03:20:14+07:00",
        "marketStatus": "Closed",
        "change": "13.250000",
        "percentChange": "0.820000",
    }


class SyncClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class AsyncClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://www.set.or.th/api/set/index/info/list?type=INDEX"
    return requests.HTTPError(f"HTTP {status_code}", response=response)


@pytest.mark.asyncio
async def test_requests_success_never_calls_playwright():
    primary = SyncClient(result=normalized_payload())
    fallback = AsyncClient(result=normalized_payload())
    result = await UnifiedSetClient(primary, fallback).fetch()
    assert result["sourceClient"] == "requests"
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 429])
async def test_allowed_http_status_falls_back_and_succeeds(status_code):
    fallback = AsyncClient(result=normalized_payload())
    result = await UnifiedSetClient(SyncClient(error=http_error(status_code)), fallback).fetch()
    assert result["sourceClient"] == "playwright"
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_connection_timeout_falls_back_and_succeeds():
    fallback = AsyncClient(result=normalized_payload())
    result = await UnifiedSetClient(SyncClient(error=requests.ConnectTimeout("timed out")), fallback).fetch()
    assert result["sourceClient"] == "playwright"
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_schema_validation_error_does_not_call_playwright():
    fallback = AsyncClient(result=normalized_payload())
    with pytest.raises(UnifiedSetClientError, match="schema error"):
        await UnifiedSetClient(SyncClient(result={"last": "1.00"}), fallback).fetch()
    assert fallback.calls == 0


def malformed_json_error():
    try:
        raise json.JSONDecodeError("bad JSON", "x", 0)
    except json.JSONDecodeError as cause:
        error = SetClientError("SET endpoint returned invalid JSON")
        error.__cause__ = cause
        return error


@pytest.mark.asyncio
async def test_malformed_json_does_not_call_playwright():
    fallback = AsyncClient(result=normalized_payload())
    with pytest.raises(UnifiedSetClientError, match="non-fallback error") as captured:
        await UnifiedSetClient(SyncClient(error=malformed_json_error()), fallback).fetch()
    assert isinstance(captured.value.__cause__, SetClientError)
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_both_clients_fail_with_types_and_fallback_as_cause():
    primary_error = requests.ConnectionError("network unavailable")
    fallback_error = RuntimeError("browser unavailable")
    with pytest.raises(UnifiedSetClientError) as captured:
        await UnifiedSetClient(
            SyncClient(error=primary_error), AsyncClient(error=fallback_error)
        ).fetch()
    assert "primary=ConnectionError" in str(captured.value)
    assert "fallback=RuntimeError" in str(captured.value)
    assert captured.value.__cause__ is fallback_error
    assert "network unavailable" not in str(captured.value)
    assert "browser unavailable" not in str(captured.value)


@pytest.mark.asyncio
async def test_returned_schema_is_identical_for_both_clients():
    requests_result = await UnifiedSetClient(
        SyncClient(result=normalized_payload()), AsyncClient(result=normalized_payload())
    ).fetch()
    playwright_result = await UnifiedSetClient(
        SyncClient(error=http_error(403)), AsyncClient(result=normalized_payload())
    ).fetch()
    expected_keys = {
        "last",
        "value",
        "marketDateTime",
        "marketStatus",
        "change",
        "percentChange",
        "sourceClient",
    }
    assert set(requests_result) == expected_keys
    assert set(playwright_result) == expected_keys
    assert all(isinstance(value, str) for value in requests_result.values())
    assert all(isinstance(value, str) for value in playwright_result.values())
