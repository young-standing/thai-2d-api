import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from dateutil import parser as date_parser
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import Settings

log = structlog.get_logger(__name__)


class SetClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class SetQuote:
    index: str
    value: str
    source_timestamp: datetime
    source: str


class SetClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch(self) -> SetQuote:
        try:
            return await self._fetch_json_with_retry()
        except Exception as exc:
            log.warning("set_json_fetch_failed", error=str(exc))
            if not self.settings.playwright_fallback_enabled:
                raise SetClientError("SET JSON fetch failed and Playwright fallback is disabled") from exc
            return await self._fetch_playwright()

    async def _fetch_json_with_retry(self) -> SetQuote:
        retrying = retry(
            stop=stop_after_attempt(self.settings.set_max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=16),
            retry=retry_if_exception_type((httpx.HTTPError, SetClientError)),
            reraise=True,
        )(self._fetch_json)
        return await retrying()

    async def _fetch_json(self) -> SetQuote:
        headers = {
            "User-Agent": self.settings.set_user_agent,
            "Accept": "application/json",
            "Referer": self.settings.set_page_url,
        }
        timeout = httpx.Timeout(self.settings.set_request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = await client.get(self.settings.set_json_url)
            response.raise_for_status()
            if "json" not in response.headers.get("content-type", "").lower():
                raise SetClientError("SET endpoint did not return JSON")
            return self._parse_json(response.json())

    def _parse_json(self, payload: Any) -> SetQuote:
        nodes = list(self._walk(payload))
        index = self._first(nodes, ("index", "last", "lastPrice", "indexValue"))
        value = self._first(nodes, ("marketValue", "totalValue", "value", "turnover"))
        timestamp = self._first(nodes, ("lastUpdate", "lastUpdated", "datetime", "timestamp", "dateTime"))
        if index is None or value is None:
            raise SetClientError("SET JSON response did not contain recognizable index/value fields")
        return SetQuote(self._as_string(index), self._as_string(value), self._as_datetime(timestamp), "json")

    @staticmethod
    def _walk(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from SetClient._walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from SetClient._walk(child)

    @staticmethod
    def _first(nodes: list[dict[str, Any]], keys: tuple[str, ...]) -> Any | None:
        for node in nodes:
            for key in keys:
                if key in node and node[key] not in (None, ""):
                    return node[key]
        return None

    @staticmethod
    def _as_string(value: Any) -> str:
        return str(value).strip().replace(",", "")

    @staticmethod
    def _as_datetime(value: Any | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if isinstance(value, (int, float)):
            divisor = 1000 if value > 10_000_000_000 else 1
            return datetime.fromtimestamp(value / divisor, tz=timezone.utc)
        parsed = date_parser.parse(str(value))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed

    async def _fetch_playwright(self) -> SetQuote:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise SetClientError("Install Playwright and its Chromium browser to enable fallback") from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.settings.playwright_headless)
            try:
                page = await browser.new_page(user_agent=self.settings.set_user_agent)
                await page.goto(
                    self.settings.set_page_url,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.set_request_timeout_seconds * 1000),
                )
                text = await page.locator("body").inner_text()
                index_match = re.search(r"(?:SET\s+)?Index\s*\n?\s*([\d,]+\.\d+)", text, re.IGNORECASE)
                value_match = re.search(r"Value\s*\([^)]*\)\s*([\d,]+\.\d+)", text, re.IGNORECASE)
                if not index_match or not value_match:
                    raise SetClientError("Could not locate SET index/value in rendered public page")
                return SetQuote(
                    index_match.group(1).replace(",", ""),
                    value_match.group(1).replace(",", ""),
                    datetime.now(timezone.utc),
                    "playwright",
                )
            finally:
                await browser.close()
