"""Manually backfill verified historical 2D results from an untrusted source."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from github_publisher import (
    BACKFILL_SESSIONS,
    MAX_HISTORY,
    YANGON,
    _default_base_url,
    _remote_json,
    merge_public_history,
    validate_public_record,
)

API_URL = "https://api.thaistock2d.com/2d_result"
SOURCE_NAME = "api.thaistock2d.com"
TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 4
RATE_LIMIT_SECONDS = 0.5
RETRY_STATUSES = {429, 500, 502, 503, 504}
DISPLAY_NUMBER = re.compile(r"^(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
TWO_DIGITS = re.compile(r"^\d{2}$")


class HistoricalBackfillError(RuntimeError):
    pass


class _NonJsonHistoryResponse(HistoricalBackfillError):
    pass


class SecondaryHistoryClient:
    """Small fail-closed client for the optional third-party history endpoint."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time_module.sleep,
    ):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "thai-2d-pages-backfill/1.0",
            }
        )

    def _fetch_query(self, query_date: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(
                    API_URL,
                    params={"date": query_date},
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    try:
                        response.raise_for_status()
                        return response.json()
                    except (requests.JSONDecodeError, ValueError) as exc:
                        raise _NonJsonHistoryResponse(
                            f"Secondary history did not return JSON for {query_date}"
                        ) from exc
                    except requests.HTTPError as exc:
                        raise HistoricalBackfillError(
                            f"Secondary history response was invalid for {query_date}"
                        ) from exc
                last_error = HistoricalBackfillError(
                    f"Secondary history returned retryable HTTP {response.status_code}"
                )

            if attempt + 1 < self.max_attempts:
                self.sleep(2**attempt)

        failure_type = type(last_error).__name__ if last_error is not None else "UnknownError"
        raise HistoricalBackfillError(
            f"Secondary history fetch failed for {query_date} after "
            f"{self.max_attempts} attempts ({failure_type})"
        ) from last_error

    def fetch_date(self, result_date: date) -> Any:
        documented_query = result_date.strftime("%d-%m-%Y")
        try:
            return self._fetch_query(documented_query)
        except _NonJsonHistoryResponse:
            # The live service has also used SQL/ISO dates despite documenting
            # DD-MM-YYYY. Retry only this narrow format mismatch, never malformed JSON.
            self.sleep(RATE_LIMIT_SECONDS)
            return self._fetch_query(result_date.isoformat())


def _source_date(value: Any) -> date:
    if not isinstance(value, str) or not value:
        raise HistoricalBackfillError("Historical record date must be a string")
    for date_format in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise HistoricalBackfillError("Historical record date has an invalid format")


def _normalized_display_number(value: Any, field: str) -> tuple[str, Decimal]:
    if not isinstance(value, str) or DISPLAY_NUMBER.fullmatch(value) is None:
        raise HistoricalBackfillError(f"Historical {field} must be a plain numeric string")
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise HistoricalBackfillError(f"Historical {field} is invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise HistoricalBackfillError(f"Historical {field} must be finite and non-negative")
    normalized_decimal = parsed.quantize(Decimal("0.01"))
    if parsed != normalized_decimal:
        raise HistoricalBackfillError(f"Historical {field} has more than two decimal places")
    return format(normalized_decimal, ".2f"), normalized_decimal


def _record_time(record: dict[str, Any]) -> str:
    supplied_time = record.get("time")
    supplied_open_time = record.get("open_time")
    if supplied_time is not None and supplied_open_time is not None:
        if supplied_time != supplied_open_time:
            raise HistoricalBackfillError("Historical time fields disagree")
    value = supplied_open_time if supplied_open_time is not None else supplied_time
    if not isinstance(value, str) or value not in BACKFILL_SESSIONS:
        raise HistoricalBackfillError("Historical record has an unsupported session")
    return value


def _build_record(
    record: dict[str, Any],
    group_date: Any,
    expected_date: date,
    imported_at: datetime,
) -> dict[str, Any]:
    record_date = _source_date(record.get("date", group_date))
    if record_date != expected_date:
        raise HistoricalBackfillError("Historical record date does not match the requested date")
    open_time = _record_time(record)
    set_index, _ = _normalized_display_number(record.get("set"), "SET")
    value_million, value_decimal = _normalized_display_number(record.get("value"), "value")
    supplied_twod = record.get("twod")
    if not isinstance(supplied_twod, str) or TWO_DIGITS.fullmatch(supplied_twod) is None:
        raise HistoricalBackfillError("Historical 2D result must be a two-character digit string")

    index_digit = set_index[-1]
    value_digit = value_million.split(".", 1)[0][-1]
    calculated = index_digit + value_digit
    if supplied_twod != calculated:
        raise HistoricalBackfillError("Historical 2D result failed local verification")

    source_datetime = datetime.combine(
        expected_date,
        time.fromisoformat(open_time),
        tzinfo=YANGON,
    ).isoformat()
    captured_yangon = imported_at.astimezone(YANGON)
    value_raw_decimal = value_decimal * Decimal("1000000")
    value_raw = format(value_raw_decimal.quantize(Decimal("1")), "f")
    return {
        "number": calculated,
        "index_digit": index_digit,
        "value_digit": value_digit,
        "set_index": set_index,
        "value_raw": value_raw,
        "value_million": value_million,
        "market_datetime": source_datetime,
        "market_status": "Historical",
        "fetched_at": captured_yangon.astimezone(timezone.utc).isoformat(),
        "source_client": SOURCE_NAME,
        "strategy": "set_hundredths_plus_displayed_value_units",
        "session": BACKFILL_SESSIONS[open_time],
        "target_time_yangon": open_time,
        "captured_at_yangon": captured_yangon.isoformat(),
        "source_market_datetime": source_datetime,
        "publication_type": "historical_backfill",
        "stale": True,
        "result_date": expected_date.isoformat(),
        "open_time": open_time,
        "source": SOURCE_NAME,
        "verified_locally": True,
    }


def parse_historical_day(
    payload: Any,
    expected_date: date,
    imported_at: datetime,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, list):
        raise HistoricalBackfillError("Historical response must be a list")
    accepted: dict[tuple[str, str], dict[str, Any]] = {}
    rejected = 0
    for group in payload:
        if not isinstance(group, dict):
            rejected += 1
            continue
        group_date = group.get("date")
        children = group.get("child")
        candidates = children if isinstance(children, list) else [group]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                rejected += 1
                continue
            try:
                normalized = _build_record(
                    candidate,
                    group_date,
                    expected_date,
                    imported_at,
                )
            except HistoricalBackfillError:
                rejected += 1
                continue
            key = (normalized["result_date"], normalized["open_time"])
            if key in accepted:
                rejected += 1
                continue
            accepted[key] = normalized
    return list(accepted.values()), rejected


class HistoricalBackfillImporter:
    def __init__(
        self,
        client: SecondaryHistoryClient | None = None,
        *,
        output_dir: str | Path = "backfill-public",
        static_dir: str | Path = "public",
        remote_loader: Callable[[str], Any] = _remote_json,
        base_url: str | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
        rate_limit_seconds: float = RATE_LIMIT_SECONDS,
    ):
        self.client = client or SecondaryHistoryClient()
        self.output_dir = Path(output_dir).resolve()
        self.static_dir = Path(static_dir).resolve()
        self.remote_loader = remote_loader
        self.base_url = base_url if base_url is not None else _default_base_url()
        self.now = now or (lambda: datetime.now(YANGON))
        self.sleep = sleep
        self.rate_limit_seconds = rate_limit_seconds

    def _published(self, filename: str) -> Any:
        if not self.base_url:
            raise HistoricalBackfillError("Published Pages base URL is not configured")
        try:
            return self.remote_loader(urljoin(self.base_url, filename))
        except Exception as exc:
            raise HistoricalBackfillError(
                f"Could not load existing published {filename}"
            ) from exc

    def _existing_records(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        latest = validate_public_record(self._published("latest.json"))
        if latest is None or latest["publication_type"] != "scheduled_result":
            raise HistoricalBackfillError("Published latest.json is not an official scheduled result")
        raw_history = self._published("history.json")
        if not isinstance(raw_history, list):
            raise HistoricalBackfillError("Published history.json must be a list")
        validated: list[dict[str, Any]] = []
        for item in raw_history:
            record = validate_public_record(item)
            if record is None:
                raise HistoricalBackfillError("Published history.json contains an invalid record")
            validated.append(record)
        return latest, merge_public_history(validated)

    def _write_artifact(
        self,
        latest: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> None:
        index_source = self.static_dir / "index.html"
        if not index_source.is_file():
            raise HistoricalBackfillError("Static index.html is missing")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        documents = {"latest.json": latest, "history.json": history}
        temporary: list[Path] = []
        try:
            for filename, document in documents.items():
                destination = self.output_dir / filename
                temp = destination.with_suffix(destination.suffix + ".tmp")
                temp.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                temporary.append(temp)
            index_temp = self.output_dir / "index.html.tmp"
            shutil.copyfile(index_source, index_temp)
            temporary.append(index_temp)
            for filename in ("history.json", "latest.json", "index.html"):
                (self.output_dir / f"{filename}.tmp").replace(self.output_dir / filename)
        finally:
            for temp in temporary:
                temp.unlink(missing_ok=True)

    def run(self, *, days: int = 30) -> dict[str, int]:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
            raise HistoricalBackfillError("Backfill days must be an integer from 1 to 30")
        imported_at = self.now()
        if imported_at.tzinfo is None or imported_at.utcoffset() is None:
            raise HistoricalBackfillError("Backfill clock must be timezone-aware")
        latest, history = self._existing_records()
        final_date = imported_at.astimezone(YANGON).date()
        first_date = final_date - timedelta(days=days - 1)
        imported: list[dict[str, Any]] = []
        rejected = 0
        for offset in range(days):
            if offset:
                self.sleep(self.rate_limit_seconds)
            requested_date = first_date + timedelta(days=offset)
            payload = self.client.fetch_date(requested_date)
            records, rejected_for_date = parse_historical_day(
                payload,
                requested_date,
                imported_at,
            )
            imported.extend(records)
            rejected += rejected_for_date

        merged = merge_public_history([*history, *imported])
        self._write_artifact(latest, merged)
        return {
            "dates_fetched": days,
            "records_imported": len(imported),
            "records_rejected": rejected,
            "history_records": len(merged),
            "history_limit": MAX_HISTORY,
        }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output-dir", default="backfill-public")
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    result = HistoricalBackfillImporter(output_dir=arguments.output_dir).run(
        days=arguments.days
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
