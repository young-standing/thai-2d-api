"""Capture SET index JSON through the public overview page with Playwright."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

SET_OVERVIEW_URL = "https://www.set.or.th/en/market/index/set/overview"
TARGET_API_PATH = "/api/set/index/info/list"
OVERALL_TIMEOUT_SECONDS = 60


class PlaywrightSetClientError(RuntimeError):
    """Raised when browser capture or SET response validation fails."""


def headless_from_environment() -> bool:
    """Run headless unless HEADLESS is explicitly set to false."""
    return os.getenv("HEADLESS", "true").strip().lower() not in {"false", "0", "no", "off"}


def _decimal_string(value: Any, field_name: str) -> str:
    if value is None:
        raise PlaywrightSetClientError(f"SET response field '{field_name}' is missing or null")
    if isinstance(value, (dict, list, bool)):
        raise PlaywrightSetClientError(
            f"SET response field '{field_name}' must be a JSON number or string"
        )
    if not isinstance(value, (str, Decimal, int)):
        raise PlaywrightSetClientError(
            f"SET response field '{field_name}' is not Decimal-safe"
        )
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise PlaywrightSetClientError(
            f"SET response field '{field_name}' is not a valid decimal"
        ) from exc
    if not number.is_finite():
        raise PlaywrightSetClientError(f"SET response field '{field_name}' must be finite")
    return format(number, "f")


def parse_set_index(payload: Any) -> dict[str, Any]:
    """Select and normalize the SET record from saved or captured JSON."""
    if not isinstance(payload, dict):
        raise PlaywrightSetClientError("SET response root must be a JSON object")

    sectors = payload.get("indexIndustrySectors")
    if not isinstance(sectors, list):
        raise PlaywrightSetClientError(
            "SET response is missing a valid 'indexIndustrySectors' list"
        )

    set_record = next(
        (record for record in sectors if isinstance(record, dict) and record.get("symbol") == "SET"),
        None,
    )
    if set_record is None:
        raise PlaywrightSetClientError(
            "SET response 'indexIndustrySectors' does not contain symbol 'SET'"
        )

    required = ("last", "value", "marketDateTime", "marketStatus", "change", "percentChange")
    missing = [field for field in required if field not in set_record]
    if missing:
        raise PlaywrightSetClientError(
            f"SET record is missing required field(s): {', '.join(missing)}"
        )

    return {
        "last": _decimal_string(set_record["last"], "last"),
        "value": _decimal_string(set_record["value"], "value"),
        "marketDateTime": set_record["marketDateTime"],
        "marketStatus": set_record["marketStatus"],
        "change": _decimal_string(set_record["change"], "change"),
        "percentChange": _decimal_string(set_record["percentChange"], "percentChange"),
    }


def _is_target_response(response: Any) -> bool:
    parsed = urlparse(response.url)
    query = parse_qs(parsed.query)
    return (
        TARGET_API_PATH in parsed.path
        and query.get("type", [""])[0].upper() == "INDEX"
        and 200 <= response.status < 300
    )


async def _capture_set_index() -> dict[str, Any]:
    """Launch Chromium and capture the successful SET index-list response."""
    async with async_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = await playwright.chromium.launch(headless=headless_from_environment())
            context = await browser.new_context()
            page = await context.new_page()

            async with page.expect_response(
                _is_target_response,
                timeout=OVERALL_TIMEOUT_SECONDS * 1000,
            ) as response_info:
                await page.goto(
                    SET_OVERVIEW_URL,
                    wait_until="domcontentloaded",
                    timeout=OVERALL_TIMEOUT_SECONDS * 1000,
                )

            response = await response_info.value
            body = await response.text()
            try:
                # Decode the captured JSON body directly. Numeric tokens become
                # strings before any float conversion can lose precision or zeros.
                payload = json.loads(body, parse_float=Decimal, parse_int=Decimal)
            except json.JSONDecodeError as exc:
                raise PlaywrightSetClientError(
                    f"Captured SET response from {response.url} was not valid JSON"
                ) from exc
            return parse_set_index(payload)
        finally:
            # Either object may already be closed after a crash or navigation
            # failure; cleanup must never hide the original capture result.
            if context is not None:
                with suppress(PlaywrightError):
                    await context.close()
            if browser is not None:
                with suppress(PlaywrightError):
                    await browser.close()


async def fetch_set_index() -> dict[str, Any]:
    """Fetch one normalized SET record within a 60-second overall deadline."""
    try:
        async with asyncio.timeout(OVERALL_TIMEOUT_SECONDS):
            return await _capture_set_index()
    except TimeoutError as exc:
        raise PlaywrightSetClientError(
            "Timed out after 60 seconds waiting for a successful SET index API response"
        ) from exc
    except PlaywrightTimeoutError as exc:
        raise PlaywrightSetClientError(
            "Timed out waiting for the SET overview page or matching API response"
        ) from exc
    except PlaywrightError as exc:
        raise PlaywrightSetClientError(f"Playwright SET capture failed: {exc}") from exc


class PlaywrightSetClient:
    """Async browser client used only when an orchestrator selects fallback."""

    async def fetch(self) -> dict[str, Any]:
        return await fetch_set_index()


async def main() -> None:
    result = await fetch_set_index()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
