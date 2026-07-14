"""Manually backfill verified historical 2D results from an untrusted source."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time as time_module
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

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


def diagnostic_log(event: str, *, level: str = "info", **fields: Any) -> None:
    """Emit allowlisted structured diagnostics without request headers or bodies."""
    document = {"event": event, "level": level, **fields}
    stream = sys.stderr if level == "error" else sys.stdout
    print(json.dumps(document, ensure_ascii=False, sort_keys=True), file=stream, flush=True)


def github_error_annotation(category: str, message: str) -> None:
    if os.getenv("GITHUB_ACTIONS", "").lower() != "true":
        return
    safe_message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(
        f"::error title=Historical backfill failed::{category}: {safe_message}",
        file=sys.stderr,
        flush=True,
    )


class HistoricalBackfillError(RuntimeError):
    def __init__(self, message: str, *, category: str = "backfill_error"):
        super().__init__(message)
        self.category = category


class _NonJsonHistoryResponse(HistoricalBackfillError):
    pass


class SecondaryHistoryClient:
    """Fail-closed HTTP client for the optional third-party history endpoint."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: float = TIMEOUT_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time_module.sleep,
        log: Callable[..., None] = diagnostic_log,
    ):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.log = log
        self.endpoint_host = urlparse(API_URL).hostname or SOURCE_NAME
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "thai-2d-pages-backfill/1.0",
            }
        )

    @staticmethod
    def _content_type(response: Any) -> str:
        headers = getattr(response, "headers", {})
        if not hasattr(headers, "get"):
            return "unknown"
        value = headers.get("Content-Type") or headers.get("content-type")
        if not isinstance(value, str) or not value:
            return "unknown"
        return value.split(";", 1)[0].strip()[:128]

    @staticmethod
    def _http_error(status: int, query_date: str) -> HistoricalBackfillError:
        if status == 403:
            category = "http_403"
        elif status == 404:
            category = "http_404"
        elif status == 429:
            category = "http_429"
        elif 500 <= status <= 599:
            category = "http_5xx"
        else:
            category = "http_error"
        return HistoricalBackfillError(
            f"Secondary history returned HTTP {status} for {query_date}",
            category=category,
        )

    def _fetch_query(self, query_date: str) -> Any:
        last_error: HistoricalBackfillError | None = None
        for attempt in range(1, self.max_attempts + 1):
            self.log(
                "http_request_started",
                endpoint_host=self.endpoint_host,
                requested_date=query_date,
                attempt=attempt,
                timeout_seconds=self.timeout,
            )
            try:
                response = self.session.get(
                    API_URL,
                    params={"date": query_date},
                    timeout=self.timeout,
                )
            except requests.exceptions.SSLError:
                last_error = HistoricalBackfillError(
                    f"TLS validation failed for {self.endpoint_host}",
                    category="tls_error",
                )
            except requests.Timeout:
                last_error = HistoricalBackfillError(
                    f"DNS/connect timeout while fetching {query_date}",
                    category="dns_or_connect_timeout",
                )
            except requests.ConnectionError:
                last_error = HistoricalBackfillError(
                    f"DNS or connection failure while fetching {query_date}",
                    category="dns_or_connect_error",
                )
            else:
                status = int(response.status_code)
                content_type = self._content_type(response)
                self.log(
                    "http_response_received",
                    endpoint_host=self.endpoint_host,
                    requested_date=query_date,
                    attempt=attempt,
                    http_status=status,
                    content_type=content_type,
                )
                if status >= 400:
                    last_error = self._http_error(status, query_date)
                    if status not in RETRY_STATUSES:
                        raise last_error
                else:
                    try:
                        return response.json()
                    except (requests.JSONDecodeError, ValueError) as exc:
                        raise _NonJsonHistoryResponse(
                            f"Secondary history returned invalid JSON for {query_date}",
                            category="invalid_json",
                        ) from exc

            if attempt < self.max_attempts:
                self.sleep(2 ** (attempt - 1))

        assert last_error is not None
        raise HistoricalBackfillError(
            f"{last_error} after {self.max_attempts} attempts",
            category=last_error.category,
        ) from last_error

    def fetch_date(self, result_date: date) -> Any:
        documented_query = result_date.strftime("%d-%m-%Y")
        try:
            return self._fetch_query(documented_query)
        except _NonJsonHistoryResponse:
            # The live service has also used SQL/ISO dates despite documenting
            # DD-MM-YYYY. Retry only this narrow format mismatch, never valid JSON
            # with an unexpected schema.
            self.log(
                "date_format_fallback",
                endpoint_host=self.endpoint_host,
                requested_date=documented_query,
                fallback_date=result_date.isoformat(),
            )
            self.sleep(RATE_LIMIT_SECONDS)
            return self._fetch_query(result_date.isoformat())


def _source_date(value: Any) -> date:
    if not isinstance(value, str) or not value:
        raise HistoricalBackfillError(
            "Historical record date must be a string",
            category="invalid_record_date",
        )
    for date_format in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise HistoricalBackfillError(
        "Historical record date has an invalid format",
        category="invalid_record_date",
    )


def _normalized_display_number(value: Any, field: str) -> tuple[str, Decimal]:
    category = "invalid_set" if field == "SET" else "invalid_value"
    if not isinstance(value, str) or DISPLAY_NUMBER.fullmatch(value) is None:
        raise HistoricalBackfillError(
            f"Historical {field} must be a plain numeric string",
            category=category,
        )
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise HistoricalBackfillError(
            f"Historical {field} is invalid",
            category=category,
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise HistoricalBackfillError(
            f"Historical {field} must be finite and non-negative",
            category=category,
        )
    normalized_decimal = parsed.quantize(Decimal("0.01"))
    if parsed != normalized_decimal:
        raise HistoricalBackfillError(
            f"Historical {field} has more than two decimal places",
            category=category,
        )
    return format(normalized_decimal, ".2f"), normalized_decimal


def _record_time(record: dict[str, Any]) -> str:
    supplied_time = record.get("time")
    supplied_open_time = record.get("open_time")
    if supplied_time is not None and supplied_open_time is not None:
        if supplied_time != supplied_open_time:
            raise HistoricalBackfillError(
                "Historical time fields disagree",
                category="unsupported_session",
            )
    value = supplied_open_time if supplied_open_time is not None else supplied_time
    if not isinstance(value, str) or value not in BACKFILL_SESSIONS:
        raise HistoricalBackfillError(
            "Historical record has an unsupported session",
            category="unsupported_session",
        )
    return value


def _build_record(
    record: dict[str, Any],
    group_date: Any,
    expected_date: date,
    imported_at: datetime,
) -> dict[str, Any]:
    record_date = _source_date(record.get("date", group_date))
    if record_date != expected_date:
        raise HistoricalBackfillError(
            "Historical record date does not match the requested date",
            category="date_mismatch",
        )
    open_time = _record_time(record)
    set_index, _ = _normalized_display_number(record.get("set"), "SET")
    value_million, value_decimal = _normalized_display_number(record.get("value"), "value")
    supplied_twod = record.get("twod")
    if not isinstance(supplied_twod, str) or TWO_DIGITS.fullmatch(supplied_twod) is None:
        raise HistoricalBackfillError(
            "Historical 2D result must be a two-character digit string",
            category="invalid_twod",
        )

    index_digit = set_index[-1]
    value_digit = value_million.split(".", 1)[0][-1]
    calculated = index_digit + value_digit
    if supplied_twod != calculated:
        raise HistoricalBackfillError(
            "Historical 2D result failed local verification",
            category="local_2d_verification_mismatch",
        )

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


@dataclass(frozen=True)
class DayParseResult:
    records: list[dict[str, Any]]
    records_parsed: int
    records_rejected: int
    rejection_reasons: dict[str, int]

    def __iter__(self):
        # Preserve the original two-value unpacking API.
        yield self.records
        yield self.records_rejected


def parse_historical_day(
    payload: Any,
    expected_date: date,
    imported_at: datetime,
) -> DayParseResult:
    if not isinstance(payload, list):
        raise HistoricalBackfillError(
            "Historical response must be a list",
            category="unexpected_response_schema",
        )
    accepted: dict[tuple[str, str], dict[str, Any]] = {}
    reasons: Counter[str] = Counter()
    parsed = 0
    for group in payload:
        if not isinstance(group, dict):
            parsed += 1
            reasons["unexpected_record_schema"] += 1
            continue
        group_date = group.get("date")
        if "child" in group and not isinstance(group["child"], list):
            parsed += 1
            reasons["unexpected_record_schema"] += 1
            continue
        candidates = group["child"] if "child" in group else [group]
        for candidate in candidates:
            parsed += 1
            if not isinstance(candidate, dict):
                reasons["unexpected_record_schema"] += 1
                continue
            try:
                normalized = _build_record(
                    candidate,
                    group_date,
                    expected_date,
                    imported_at,
                )
            except HistoricalBackfillError as exc:
                reasons[exc.category] += 1
                continue
            key = (normalized["result_date"], normalized["open_time"])
            if key in accepted:
                reasons["duplicate_session"] += 1
                continue
            accepted[key] = normalized
    return DayParseResult(
        records=list(accepted.values()),
        records_parsed=parsed,
        records_rejected=sum(reasons.values()),
        rejection_reasons=dict(sorted(reasons.items())),
    )


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
        log: Callable[..., None] = diagnostic_log,
    ):
        self.log = log
        self.client = client or SecondaryHistoryClient(log=log)
        self.output_dir = Path(output_dir).resolve()
        self.static_dir = Path(static_dir).resolve()
        self.remote_loader = remote_loader
        self.base_url = base_url if base_url is not None else _default_base_url()
        self.now = now or (lambda: datetime.now(YANGON))
        self.sleep = sleep
        self.rate_limit_seconds = rate_limit_seconds
        self.report: dict[str, Any] = {
            "status": "not_started",
            "endpoint_host": SOURCE_NAME,
            "dates_requested": 0,
            "dates_completed": 0,
            "records_parsed": 0,
            "records_accepted": 0,
            "records_rejected": 0,
            "rejection_reasons": {},
        }

    def _published(self, filename: str) -> Any:
        if not self.base_url:
            raise HistoricalBackfillError(
                "Published Pages base URL is not configured",
                category="published_data_error",
            )
        try:
            return self.remote_loader(urljoin(self.base_url, filename))
        except Exception as exc:
            raise HistoricalBackfillError(
                f"Could not load existing published {filename}",
                category="published_data_error",
            ) from exc

    def _existing_records(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        latest = validate_public_record(self._published("latest.json"))
        if latest is None or latest["publication_type"] != "scheduled_result":
            raise HistoricalBackfillError(
                "Published latest.json is not an official scheduled result",
                category="published_data_error",
            )
        raw_history = self._published("history.json")
        if not isinstance(raw_history, list):
            raise HistoricalBackfillError(
                "Published history.json must be a list",
                category="published_data_error",
            )
        validated: list[dict[str, Any]] = []
        for item in raw_history:
            record = validate_public_record(item)
            if record is None:
                raise HistoricalBackfillError(
                    "Published history.json contains an invalid record",
                    category="published_data_error",
                )
            validated.append(record)
        return latest, merge_public_history(validated)

    def _write_artifact(
        self,
        latest: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> None:
        index_source = self.static_dir / "index.html"
        if not index_source.is_file():
            raise HistoricalBackfillError(
                "Static index.html is missing; output was not changed",
                category="output_file_failure",
            )
        parent = self.output_dir.parent
        staging: Path | None = None
        backup: Path | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{self.output_dir.name}-", dir=parent))
            for filename, document in {"latest.json": latest, "history.json": history}.items():
                (staging / filename).write_text(
                    json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            shutil.copyfile(index_source, staging / "index.html")

            if self.output_dir.exists():
                backup = Path(tempfile.mkdtemp(prefix=f".{self.output_dir.name}-backup-", dir=parent))
                backup.rmdir()
                self.output_dir.replace(backup)
            staging.replace(self.output_dir)
            staging = None
        except OSError as exc:
            if backup is not None and backup.exists() and not self.output_dir.exists():
                try:
                    backup.replace(self.output_dir)
                    backup = None
                except OSError:
                    pass
            raise HistoricalBackfillError(
                "Could not write the backfill artifact; previous files were preserved",
                category="output_file_failure",
            ) from exc
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)

    def _record_failure(self, exc: HistoricalBackfillError) -> None:
        self.report.update(
            {
                "status": "failed",
                "failure_category": exc.category,
                "failure_message": str(exc),
            }
        )

    def run(self, *, days: int = 30) -> dict[str, Any]:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
            raise HistoricalBackfillError(
                "Backfill days must be an integer from 1 to 30",
                category="configuration_error",
            )
        imported_at = self.now()
        if imported_at.tzinfo is None or imported_at.utcoffset() is None:
            raise HistoricalBackfillError(
                "Backfill clock must be timezone-aware",
                category="configuration_error",
            )
        final_date = imported_at.astimezone(YANGON).date()
        first_date = final_date - timedelta(days=days - 1)
        self.report.update(
            {
                "status": "running",
                "requested_start_date": first_date.isoformat(),
                "requested_end_date": final_date.isoformat(),
                "dates_requested": days,
            }
        )
        self.log(
            "backfill_started",
            endpoint_host=SOURCE_NAME,
            requested_start_date=first_date.isoformat(),
            requested_end_date=final_date.isoformat(),
            dates_requested=days,
        )

        rejection_reasons: Counter[str] = Counter()
        imported: list[dict[str, Any]] = []
        try:
            latest, history = self._existing_records()
            for offset in range(days):
                if offset:
                    self.sleep(self.rate_limit_seconds)
                requested_date = first_date + timedelta(days=offset)
                self.log(
                    "date_fetch_started",
                    endpoint_host=SOURCE_NAME,
                    current_date=requested_date.isoformat(),
                )
                payload = self.client.fetch_date(requested_date)
                parsed = parse_historical_day(payload, requested_date, imported_at)
                imported.extend(parsed.records)
                rejection_reasons.update(parsed.rejection_reasons)
                self.report["dates_completed"] += 1
                self.report["records_parsed"] += parsed.records_parsed
                self.report["records_accepted"] += len(parsed.records)
                self.report["records_rejected"] += parsed.records_rejected
                self.report["rejection_reasons"] = dict(sorted(rejection_reasons.items()))
                self.log(
                    "date_parsed",
                    endpoint_host=SOURCE_NAME,
                    current_date=requested_date.isoformat(),
                    records_parsed=parsed.records_parsed,
                    records_accepted=len(parsed.records),
                    records_rejected=parsed.records_rejected,
                    rejection_reasons=parsed.rejection_reasons,
                )

            if not imported:
                if rejection_reasons.get("local_2d_verification_mismatch"):
                    raise HistoricalBackfillError(
                        "No valid records: candidate results failed local 2D verification",
                        category="local_2d_verification_mismatch",
                    )
                raise HistoricalBackfillError(
                    "No valid historical records were returned for the requested range",
                    category="no_valid_records",
                )

            merged = merge_public_history([*history, *imported])
            self._write_artifact(latest, merged)
            self.report.update(
                {
                    "status": "success",
                    "records_imported": len(imported),
                    "history_records": len(merged),
                    "history_limit": MAX_HISTORY,
                }
            )
            self.log(
                "backfill_completed",
                endpoint_host=SOURCE_NAME,
                records_parsed=self.report["records_parsed"],
                records_accepted=self.report["records_accepted"],
                records_rejected=self.report["records_rejected"],
                rejection_reasons=self.report["rejection_reasons"],
                history_records=len(merged),
            )
            return dict(self.report)
        except HistoricalBackfillError as exc:
            self._record_failure(exc)
            raise


def render_step_summary(report: dict[str, Any]) -> str:
    status = report.get("status", "failed")
    lines = [
        "### Historical 2D backfill",
        f"- Status: {status}",
        f"- Endpoint host: {report.get('endpoint_host', SOURCE_NAME)}",
        f"- Requested range: {report.get('requested_start_date', 'unknown')} to "
        f"{report.get('requested_end_date', 'unknown')}",
        f"- Dates completed: {report.get('dates_completed', 0)} / "
        f"{report.get('dates_requested', 0)}",
        f"- Records parsed: {report.get('records_parsed', 0)}",
        f"- Records accepted: {report.get('records_accepted', 0)}",
        f"- Records rejected: {report.get('records_rejected', 0)}",
    ]
    reasons = report.get("rejection_reasons")
    if isinstance(reasons, dict) and reasons:
        lines.append("- Rejection reasons:")
        for reason, count in sorted(reasons.items()):
            lines.append(f"  - {reason}: {count}")
    if status != "success":
        lines.append(f"- Failure category: {report.get('failure_category', 'report_unavailable')}")
        lines.append(f"- Failure: {report.get('failure_message', 'Backfill command failed')}")
        lines.append("- Published JSON: unchanged")
    else:
        lines.append(f"- Final history records: {report.get('history_records', 0)}")
        lines.append("- Official scheduled records retained priority")
    return "\n".join(lines) + "\n"


def _write_report(path: str | Path, report: dict[str, Any]) -> None:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temp.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output-dir", default="backfill-public")
    parser.add_argument("--report-path")
    parser.add_argument("--render-summary")
    return parser.parse_args(argv)


def cli(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    if arguments.render_summary:
        try:
            value = json.loads(Path(arguments.render_summary).read_text(encoding="utf-8"))
            report = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            report = {
                "status": "failed",
                "endpoint_host": SOURCE_NAME,
                "failure_category": "report_unavailable",
                "failure_message": "Backfill diagnostic report was unavailable",
            }
        print(render_step_summary(report), end="")
        return 0

    importer = HistoricalBackfillImporter(output_dir=arguments.output_dir)
    exit_code = 0
    try:
        importer.run(days=arguments.days)
    except HistoricalBackfillError as exc:
        importer._record_failure(exc)
        diagnostic_log(
            "backfill_failed",
            level="error",
            endpoint_host=SOURCE_NAME,
            failure_category=exc.category,
            error=str(exc),
        )
        github_error_annotation(exc.category, str(exc))
        exit_code = 1
    except Exception:
        safe_error = HistoricalBackfillError(
            "Unexpected internal backfill failure",
            category="unexpected_internal_error",
        )
        importer._record_failure(safe_error)
        diagnostic_log(
            "backfill_failed",
            level="error",
            endpoint_host=SOURCE_NAME,
            failure_category=safe_error.category,
            error=str(safe_error),
        )
        github_error_annotation(safe_error.category, str(safe_error))
        exit_code = 1

    if arguments.report_path:
        try:
            _write_report(arguments.report_path, importer.report)
        except OSError:
            diagnostic_log(
                "backfill_failed",
                level="error",
                endpoint_host=SOURCE_NAME,
                failure_category="diagnostic_output_failure",
                error="Could not write the backfill diagnostic report",
            )
            github_error_annotation(
                "diagnostic_output_failure",
                "Could not write the backfill diagnostic report",
            )
            exit_code = 1
    if exit_code == 0:
        print(json.dumps(importer.report, indent=2, sort_keys=True))
    return exit_code


def main() -> None:
    raise SystemExit(cli())


if __name__ == "__main__":
    main()
