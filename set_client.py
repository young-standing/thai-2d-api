"""Fetch the SET index record from SET's public index-list JSON endpoint."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SET_INDEX_URL = "https://www.set.or.th/api/set/index/info/list?type=INDEX"
TIMEOUT_SECONDS = 15
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


class SetClientError(RuntimeError):
    """Raised when SET data cannot be fetched or has an unexpected shape."""


def create_session() -> requests.Session:
    """Create a retry-enabled HTTP session without cookies or authentication."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=1.0,
        status_forcelist=RETRY_STATUS_CODES,
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.set.or.th/en/market/index/set/overview",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _safe_decimal_string(value: Any, field_name: str) -> str:
    """Normalize a numeric value to plain Decimal notation without using float."""
    if value is None:
        raise SetClientError(f"SET response field '{field_name}' is missing or null")
    if isinstance(value, (dict, list, bool)):
        raise SetClientError(f"SET response field '{field_name}' is not a number or string")
    if not isinstance(value, (str, Decimal, int)):
        raise SetClientError(f"SET response field '{field_name}' is not Decimal-safe")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise SetClientError(f"SET response field '{field_name}' is not a valid decimal") from exc
    if not number.is_finite():
        raise SetClientError(f"SET response field '{field_name}' must be finite")
    return format(number, "f")


def parse_set_index(payload: Any) -> dict[str, Any]:
    """Extract and normalize the SET index object from a decoded response."""
    if not isinstance(payload, dict):
        raise SetClientError("SET response root must be a JSON object")

    sectors = payload.get("indexIndustrySectors")
    if not isinstance(sectors, list):
        raise SetClientError("SET response is missing the 'indexIndustrySectors' array")

    set_record = next(
        (item for item in sectors if isinstance(item, dict) and item.get("symbol") == "SET"),
        None,
    )
    if set_record is None:
        raise SetClientError("SET response 'indexIndustrySectors' does not contain symbol 'SET'")

    required_fields = ("last", "value", "marketDateTime", "marketStatus", "change", "percentChange")
    missing = [field for field in required_fields if field not in set_record]
    if missing:
        raise SetClientError(f"SET record is missing required field(s): {', '.join(missing)}")

    return {
        "last": _safe_decimal_string(set_record["last"], "last"),
        "value": _safe_decimal_string(set_record["value"], "value"),
        "marketDateTime": set_record["marketDateTime"],
        "marketStatus": set_record["marketStatus"],
        "change": _safe_decimal_string(set_record["change"], "change"),
        "percentChange": _safe_decimal_string(set_record["percentChange"], "percentChange"),
    }


def fetch_set_index(session: requests.Session | None = None) -> dict[str, Any]:
    """Fetch SET data once and return the normalized SET index fields."""
    owns_session = session is None
    http = session or create_session()
    try:
        response = http.get(SET_INDEX_URL, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        try:
            # Parsing numeric JSON tokens as strings avoids an intermediate float.
            payload = json.loads(response.text, parse_float=Decimal, parse_int=Decimal)
        except json.JSONDecodeError as exc:
            raise SetClientError("SET endpoint returned invalid JSON") from exc
        return parse_set_index(payload)
    except requests.RequestException as exc:
        raise SetClientError(f"SET request failed: {exc}") from exc
    finally:
        if owns_session:
            http.close()


class SetClient:
    """Synchronous requests-based SET client used as the primary transport."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session

    def fetch(self) -> dict[str, Any]:
        return fetch_set_index(self.session)


def main() -> None:
    result = fetch_set_index()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
