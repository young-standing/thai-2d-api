from __future__ import annotations

import requests
import pytest

from glo_client import (
    GloClient,
    GloClientError,
    GloSchemaError,
    GloTransportError,
    parse_official_payload,
)


def payload(draw="2026-07-16", number="100007"):
    return {
        "status": True,
        "response": {
            "date": draw,
            "data": {"first": {"number": [{"round": 1, "value": number}]}},
        },
    }


def test_official_json_response_parsing():
    result = parse_official_payload(
        payload(), fetched_at="2026-07-16T10:00:00+00:00"
    )
    assert result["draw_date"] == "2026-07-16"
    assert result["first_prize"] == "100007"
    assert result["source_updated_at"] is None
    assert result["source_client"] == "http"


@pytest.mark.parametrize("bad", [{}, {"status": True, "response": {"date": "2026-07-16", "data": {}}}])
def test_missing_first_prize(bad):
    with pytest.raises(GloSchemaError):
        parse_official_payload(bad)


def test_malformed_draw_date():
    with pytest.raises(GloSchemaError):
        parse_official_payload(payload(draw="16-07-2026"))


class Response:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, *args, **kwargs):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_http_timeout_uses_playwright_fallback():
    expected = parse_official_payload(payload(), source_client="playwright")

    async def fallback():
        return expected

    client = GloClient(
        session=Session([requests.Timeout("safe")]), retries=0, playwright_fetcher=fallback
    )
    assert await client.fetch() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500, 501, 502, 503, 504, 599])
async def test_retryable_http_status_then_success(status):
    session = Session([Response(status), Response(200, payload())])
    client = GloClient(session=session, retries=1, backoff_seconds=0, sleep=lambda _: None)
    result = await client.fetch()
    assert result["first_prize"] == "100007"
    assert session.calls == 2


@pytest.mark.asyncio
async def test_playwright_fallback_success():
    expected = parse_official_payload(payload(number="000123"), source_client="playwright")

    async def fallback():
        return expected

    client = GloClient(session=Session([Response(403)]), retries=0, playwright_fetcher=fallback)
    assert (await client.fetch())["source_client"] == "playwright"


@pytest.mark.asyncio
async def test_both_source_clients_failing_has_safe_error():
    async def fallback():
        raise GloTransportError("browser failed")

    client = GloClient(session=Session([Response(403)]), retries=0, playwright_fetcher=fallback)
    with pytest.raises(GloClientError, match="http=GloTransportError.*playwright=GloTransportError"):
        await client.fetch()


@pytest.mark.asyncio
async def test_schema_error_does_not_use_fallback():
    called = False

    async def fallback():
        nonlocal called
        called = True
        return {}

    client = GloClient(session=Session([Response(200, payload(number="12345"))]), playwright_fetcher=fallback)
    with pytest.raises(GloSchemaError):
        await client.fetch()
    assert called is False
