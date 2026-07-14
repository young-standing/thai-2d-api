"""Asynchronous client for official Thailand Government Lottery results."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import suppress
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import requests
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

OFFICIAL_API_URL = "https://www.glo.or.th/api/lottery/getLatestLottery"
OFFICIAL_PAGE_URL = "https://www.glo.or.th/mission/reward-payment/check-reward"
APPROVED_OFFICIAL_HOSTS = frozenset({"glo.or.th", "www.glo.or.th"})
TIMEOUT_SECONDS = 15
PLAYWRIGHT_TIMEOUT_SECONDS = 60
RETRY_STATUSES = frozenset({403, 429})
_SIX_DIGITS = re.compile(r"^[0-9]{6}$", re.ASCII)
_NORMALIZED_FIELDS = frozenset(
    {
        "draw_date",
        "first_prize",
        "source_updated_at",
        "fetched_at",
        "source",
        "source_client",
    }
)


class GloClientError(RuntimeError):
    """Base safe client error."""


class GloTransportError(GloClientError):
    """HTTP/network failure for which browser fallback is allowed."""


class GloSchemaError(GloClientError):
    """Official response failed strict data validation."""


def _aware_iso(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise GloSchemaError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GloSchemaError(f"{field} must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GloSchemaError(f"{field} must be timezone-aware")
    return value


def _approved_source(url: str) -> str:
    if not isinstance(url, str):
        raise GloSchemaError("source must be an official HTTPS URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in APPROVED_OFFICIAL_HOSTS:
        raise GloSchemaError("source must use an approved official GLO host")
    return url


def parse_official_payload(
    payload: Any,
    *,
    source: str = OFFICIAL_API_URL,
    source_client: str = "http",
    fetched_at: str | None = None,
) -> dict[str, str | None]:
    """Validate the documented GLO payload path and normalize one result."""
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise GloSchemaError("official response status is missing or unsuccessful")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise GloSchemaError("official response object is missing")
    draw_date = response.get("date")
    if not isinstance(draw_date, str):
        raise GloSchemaError("official draw date is missing")
    try:
        parsed_date = date.fromisoformat(draw_date)
    except ValueError as exc:
        raise GloSchemaError("official draw date is not valid YYYY-MM-DD") from exc
    if parsed_date.isoformat() != draw_date:
        raise GloSchemaError("official draw date is not canonical YYYY-MM-DD")

    data = response.get("data")
    first = data.get("first") if isinstance(data, dict) else None
    numbers = first.get("number") if isinstance(first, dict) else None
    if not isinstance(numbers, list) or not numbers or not isinstance(numbers[0], dict):
        raise GloSchemaError("official first-prize result is missing")
    first_prize = numbers[0].get("value")
    if not isinstance(first_prize, str) or _SIX_DIGITS.fullmatch(first_prize) is None:
        raise GloSchemaError("official first-prize result must be six ASCII digits")

    updated = response.get("updated_at", response.get("updatedAt"))
    normalized_updated = _aware_iso(updated, "source_updated_at", nullable=True)
    normalized_fetched = fetched_at or datetime.now(timezone.utc).isoformat()
    _aware_iso(normalized_fetched, "fetched_at")
    if source_client not in {"http", "playwright"}:
        raise GloSchemaError("source_client must be http or playwright")
    return {
        "draw_date": draw_date,
        "first_prize": first_prize,
        "source_updated_at": normalized_updated,
        "fetched_at": normalized_fetched,
        "source": _approved_source(source),
        "source_client": source_client,
    }


def validate_normalized_result(value: Any) -> dict[str, str | None]:
    """Enforce the exact public client contract, including injected fallbacks."""
    if not isinstance(value, dict) or set(value) != _NORMALIZED_FIELDS:
        raise GloSchemaError("normalized GLO result has an unexpected schema")
    draw = value["draw_date"]
    try:
        parsed_draw = date.fromisoformat(draw) if isinstance(draw, str) else None
    except ValueError as exc:
        raise GloSchemaError("normalized draw_date is invalid") from exc
    if parsed_draw is None or parsed_draw.isoformat() != draw:
        raise GloSchemaError("normalized draw_date is invalid")
    prize = value["first_prize"]
    if not isinstance(prize, str) or _SIX_DIGITS.fullmatch(prize) is None:
        raise GloSchemaError("normalized first_prize must be six ASCII digits")
    _aware_iso(value["fetched_at"], "fetched_at")
    _aware_iso(value["source_updated_at"], "source_updated_at", nullable=True)
    _approved_source(value["source"])
    if value["source_client"] not in {"http", "playwright"}:
        raise GloSchemaError("normalized source_client is invalid")
    return dict(value)


class GloClient:
    """Try the official JSON endpoint, then its public page in Chromium."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        playwright_fetcher: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._session = session or requests.Session()
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._playwright_fetcher = playwright_fetcher or self._fetch_playwright

    def _fetch_http(self) -> dict[str, str | None]:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json",
        }
        last_error: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._session.post(
                    OFFICIAL_API_URL,
                    headers=headers,
                    timeout=TIMEOUT_SECONDS,
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            else:
                if 200 <= response.status_code < 300:
                    try:
                        payload = response.json()
                    except (requests.JSONDecodeError, ValueError) as exc:
                        raise GloSchemaError("official endpoint returned invalid JSON") from exc
                    return parse_official_payload(payload, source_client="http")
                if response.status_code not in RETRY_STATUSES and not (
                    500 <= response.status_code <= 599
                ):
                    raise GloTransportError(
                        f"official endpoint returned HTTP {response.status_code}"
                    )
                last_error = GloTransportError(
                    f"official endpoint returned HTTP {response.status_code}"
                )
            if attempt < self._retries:
                self._sleep(self._backoff_seconds * (2**attempt))
        raise GloTransportError("official endpoint request failed after retries") from last_error

    async def _fetch_playwright(self) -> dict[str, str | None]:
        headless = os.getenv("HEADLESS", "true").strip().lower() not in {"false", "0", "no"}
        async with async_playwright() as playwright:
            browser = context = None
            try:
                browser = await playwright.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()

                def matches(response: Any) -> bool:
                    parsed = urlparse(response.url)
                    return (
                        parsed.hostname in APPROVED_OFFICIAL_HOSTS
                        and parsed.path == urlparse(OFFICIAL_API_URL).path
                        and 200 <= response.status < 300
                    )

                async with page.expect_response(matches, timeout=PLAYWRIGHT_TIMEOUT_SECONDS * 1000) as info:
                    await page.goto(
                        OFFICIAL_PAGE_URL,
                        wait_until="domcontentloaded",
                        timeout=PLAYWRIGHT_TIMEOUT_SECONDS * 1000,
                    )
                response = await info.value
                body = await response.text()
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise GloSchemaError("captured official response was invalid JSON") from exc
                return parse_official_payload(
                    payload, source=response.url, source_client="playwright"
                )
            except GloSchemaError:
                raise
            except (PlaywrightError, TimeoutError) as exc:
                raise GloTransportError("official Playwright capture failed") from exc
            finally:
                if context is not None:
                    with suppress(PlaywrightError):
                        await context.close()
                if browser is not None:
                    with suppress(PlaywrightError):
                        await browser.close()

    async def fetch(self) -> dict[str, str | None]:
        try:
            return validate_normalized_result(await asyncio.to_thread(self._fetch_http))
        except GloSchemaError:
            raise
        except GloTransportError as primary:
            try:
                async with asyncio.timeout(PLAYWRIGHT_TIMEOUT_SECONDS):
                    return validate_normalized_result(await self._playwright_fetcher())
            except Exception as fallback:
                raise GloClientError(
                    "official GLO clients failed "
                    f"(http={type(primary).__name__}, playwright={type(fallback).__name__})"
                ) from fallback


async def main() -> None:
    print(json.dumps(await GloClient().fetch(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
