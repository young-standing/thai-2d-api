"""Unified async SET client with a narrowly controlled browser fallback."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, Literal, Protocol

import requests

from playwright_set_client import PlaywrightSetClient
from set_client import SetClient

OUTPUT_FIELDS = (
    "last",
    "value",
    "marketDateTime",
    "marketStatus",
    "change",
    "percentChange",
)


class UnifiedSetClientError(RuntimeError):
    """Raised when unified client orchestration or output validation fails."""


class SyncSetClient(Protocol):
    def fetch(self) -> dict[str, Any]: ...


class AsyncSetClient(Protocol):
    async def fetch(self) -> dict[str, Any]: ...


def _exception_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _fallback_allowed(error: BaseException) -> bool:
    """Return true only for explicitly approved primary transport failures."""
    for cause in _exception_chain(error):
        if isinstance(cause, requests.HTTPError):
            status_code = cause.response.status_code if cause.response is not None else None
            if status_code in {403, 429}:
                return True
        if isinstance(cause, (requests.ConnectionError, requests.Timeout, socket.gaierror)):
            return True
    return False


def _validated_output(payload: Any, source: Literal["requests", "playwright"]) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise UnifiedSetClientError(f"{source} client returned a non-object result")

    missing = [field for field in OUTPUT_FIELDS if field not in payload]
    extra = [field for field in payload if field not in OUTPUT_FIELDS]
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise UnifiedSetClientError(f"{source} client schema error ({'; '.join(details)})")

    non_strings = [field for field in OUTPUT_FIELDS if not isinstance(payload[field], str)]
    if non_strings:
        raise UnifiedSetClientError(
            f"{source} client returned non-string fields: {', '.join(non_strings)}"
        )

    return {**{field: payload[field] for field in OUTPUT_FIELDS}, "sourceClient": source}


class UnifiedSetClient:
    """Try requests first and use one Playwright fetch for allowed failures only."""

    def __init__(
        self,
        requests_client: SyncSetClient | None = None,
        playwright_client: AsyncSetClient | None = None,
    ):
        self.requests_client = requests_client or SetClient()
        self.playwright_client = playwright_client or PlaywrightSetClient()

    async def fetch(self) -> dict[str, str]:
        try:
            primary_result = await asyncio.to_thread(self.requests_client.fetch)
        except Exception as primary_error:
            if not _fallback_allowed(primary_error):
                raise UnifiedSetClientError(
                    f"Requests SET client failed with non-fallback error: {type(primary_error).__name__}"
                ) from primary_error

            try:
                fallback_result = await self.playwright_client.fetch()
            except Exception as fallback_error:
                raise UnifiedSetClientError(
                    "Both SET clients failed "
                    f"(primary={type(primary_error).__name__}, "
                    f"fallback={type(fallback_error).__name__})"
                ) from fallback_error
            return _validated_output(fallback_result, "playwright")

        # Validation is deliberately outside the transport exception handler:
        # invalid primary data must never trigger the browser fallback.
        return _validated_output(primary_result, "requests")


async def main() -> None:
    result = await UnifiedSetClient().fetch()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
