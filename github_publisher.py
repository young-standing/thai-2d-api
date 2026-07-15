"""Publish verified Myanmar 2D results as static GitHub Pages JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from two_d_service import MyanmarTwoDStrategy
from unified_set_client import UnifiedSetClient

YANGON = ZoneInfo("Asia/Yangon")
BANGKOK = ZoneInfo("Asia/Bangkok")
SessionName = Literal["morning", "evening"]
TARGETS = {"morning": time(12, 1), "evening": time(16, 30)}
WINDOW_ENDS = {"morning": time(12, 6), "evening": time(16, 35)}
POLL_SECONDS = 30
MAX_HISTORY = 200
MAX_REMOTE_BYTES = 1_000_000
PUBLIC_FIELDS = (
    "number",
    "index_digit",
    "value_digit",
    "set_index",
    "value_raw",
    "value_million",
    "market_datetime",
    "market_status",
    "fetched_at",
    "source_client",
    "strategy",
    "session",
    "target_time_yangon",
    "captured_at_yangon",
    "source_market_datetime",
    "publication_type",
    "stale",
)
BACKFILL_EXTRA_FIELDS = (
    "result_date",
    "open_time",
    "source",
    "verified_locally",
)
BACKFILL_PUBLIC_FIELDS = PUBLIC_FIELDS + BACKFILL_EXTRA_FIELDS
BACKFILL_SESSIONS = {
    "11:00:00": "morning_open",
    "12:01:00": "morning",
    "15:00:00": "afternoon_open",
    "16:30:00": "evening",
}
TWO_DIGITS = re.compile(r"^\d{2}$")
ONE_DIGIT = re.compile(r"^\d$")
DECIMAL_STRING = re.compile(r"^\d+\.\d+$")
INTEGER_STRING = re.compile(r"^\d+$")


class GitHubPublisherError(RuntimeError):
    pass


def _json_log(event: dict[str, Any]) -> None:
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


def _default_base_url() -> str | None:
    configured = os.getenv("PUBLISHED_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/") + "/"
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        return None
    owner, name = repository.split("/", 1)
    return f"https://{owner}.github.io/{name}/"


def _remote_json(url: str) -> Any:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GitHubPublisherError("Published history URL must use HTTPS")
    response = requests.get(
        url,
        headers={"Accept": "application/json", "User-Agent": "thai-2d-pages-publisher/1.0"},
        timeout=10,
    )
    response.raise_for_status()
    if len(response.content) > MAX_REMOTE_BYTES:
        raise GitHubPublisherError("Published JSON exceeds the size limit")
    return response.json()


def validate_public_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    publication_type = value.get("publication_type")
    if publication_type == "scheduled_result":
        expected_fields = PUBLIC_FIELDS
    elif publication_type == "historical_backfill":
        expected_fields = BACKFILL_PUBLIC_FIELDS
    else:
        return None
    if set(value) != set(expected_fields):
        return None
    record: dict[str, Any] = {}
    for field in expected_fields:
        item = value[field]
        if field in {"stale", "verified_locally"}:
            if not isinstance(item, bool):
                return None
        elif not isinstance(item, str):
            return None
        record[field] = item
    if record["source_market_datetime"] != record["market_datetime"]:
        return None
    if publication_type == "scheduled_result":
        if record["session"] not in {"morning", "evening"}:
            return None
        if record["target_time_yangon"] != TARGETS[record["session"]].isoformat():
            return None
        if record["source_client"] not in {"requests", "playwright"}:
            return None
        if record["strategy"] != "set_hundredths_plus_value_million_units":
            return None
    else:
        open_time = record["open_time"]
        if open_time not in BACKFILL_SESSIONS:
            return None
        if record["session"] != BACKFILL_SESSIONS[open_time]:
            return None
        if record["target_time_yangon"] != open_time:
            return None
        if record["source_client"] != "api.thaistock2d.com":
            return None
        if record["source"] != "api.thaistock2d.com":
            return None
        if record["verified_locally"] is not True:
            return None
        if record["strategy"] != "set_hundredths_plus_displayed_value_units":
            return None
        try:
            result_date = date.fromisoformat(record["result_date"])
        except ValueError:
            return None
    if TWO_DIGITS.fullmatch(record["number"]) is None:
        return None
    if ONE_DIGIT.fullmatch(record["index_digit"]) is None:
        return None
    if ONE_DIGIT.fullmatch(record["value_digit"]) is None:
        return None
    if record["number"] != record["index_digit"] + record["value_digit"]:
        return None
    if DECIMAL_STRING.fullmatch(record["set_index"]) is None:
        return None
    if INTEGER_STRING.fullmatch(record["value_raw"]) is None:
        return None
    if DECIMAL_STRING.fullmatch(record["value_million"]) is None:
        return None
    if not record["market_status"] or len(record["market_status"]) > 64:
        return None
    try:
        market_time = datetime.fromisoformat(record["market_datetime"])
        fetched_time = datetime.fromisoformat(record["fetched_at"])
        captured_time = datetime.fromisoformat(record["captured_at_yangon"])
    except ValueError:
        return None
    if any(value.tzinfo is None or value.utcoffset() is None for value in (
        market_time,
        fetched_time,
        captured_time,
    )):
        return None
    if publication_type == "scheduled_result":
        source_yangon = market_time.astimezone(BANGKOK).astimezone(YANGON)
        capture_yangon = captured_time.astimezone(YANGON)
        window_start = datetime.combine(
            source_yangon.date(), TARGETS[record["session"]], tzinfo=YANGON
        )
        window_end = datetime.combine(
            source_yangon.date(), WINDOW_ENDS[record["session"]], tzinfo=YANGON
        )
        if not window_start <= source_yangon <= window_end:
            return None
        if capture_yangon.date() != source_yangon.date():
            return None
    else:
        if market_time.astimezone(YANGON).date() != result_date:
            return None
        if market_time.astimezone(YANGON).time().replace(tzinfo=None) != time.fromisoformat(record["open_time"]):
            return None
    return record


def record_identity(record: dict[str, Any]) -> tuple[str, str]:
    if record.get("publication_type") == "historical_backfill":
        return record["result_date"], record["open_time"]
    source = datetime.fromisoformat(record["source_market_datetime"]).astimezone(YANGON)
    return source.date().isoformat(), record["target_time_yangon"]


def _record_timestamp(record: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(record["market_datetime"]).astimezone(timezone.utc)


def merge_public_history(
    records: list[dict[str, Any]],
    *,
    limit: int | None = MAX_HISTORY,
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = record_identity(record)
        existing = merged.get(key)
        if existing is None or (
            record["publication_type"] == "scheduled_result"
            and existing["publication_type"] != "scheduled_result"
        ):
            merged[key] = record
    ordered = sorted(merged.values(), key=_record_timestamp, reverse=True)
    return ordered if limit is None else ordered[:limit]


def validate_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    validated: list[dict[str, Any]] = []
    for item in value:
        record = validate_public_record(item)
        if record is None:
            return []
        validated.append(record)
    return merge_public_history(validated)


def _previous_weekday(value: date) -> date:
    previous = value - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def expected_scheduled_result(current: datetime) -> tuple[date, SessionName]:
    """Return the market date and session expected at a Yangon-local instant."""
    if current.tzinfo is None or current.utcoffset() is None:
        raise GitHubPublisherError("Stale-check time must be timezone-aware")
    local = current.astimezone(YANGON)
    if local.weekday() >= 5:
        return _previous_weekday(local.date()), "evening"
    if local.time() >= TARGETS["evening"]:
        return local.date(), "evening"
    if local.time() >= TARGETS["morning"]:
        return local.date(), "morning"
    return _previous_weekday(local.date()), "evening"


def is_scheduled_result_stale(record: dict[str, Any], current: datetime) -> bool:
    expected_date, expected_session = expected_scheduled_result(current)
    try:
        source = datetime.fromisoformat(record["source_market_datetime"])
    except (KeyError, TypeError, ValueError):
        return True
    if source.tzinfo is None or source.utcoffset() is None:
        return True
    return (
        record.get("publication_type") != "scheduled_result"
        or record.get("session") != expected_session
        or source.astimezone(YANGON).date() != expected_date
    )


class GitHubPublisher:
    def __init__(
        self,
        client: UnifiedSetClient | None = None,
        *,
        output_dir: str | Path = "public",
        remote_loader: Callable[[str], Any] = _remote_json,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        base_url: str | None = None,
        log: Callable[[dict[str, Any]], None] = _json_log,
    ):
        self.client = client or UnifiedSetClient()
        self.output_dir = Path(output_dir).resolve()
        self.remote_loader = remote_loader
        self.now = now or (lambda: datetime.now(YANGON))
        self.sleep = sleep
        self.base_url = base_url if base_url is not None else _default_base_url()
        self.log = log

    def _path(self, filename: str) -> Path:
        if filename not in {"latest.json", "history.json"}:
            raise GitHubPublisherError("Unsafe static output filename")
        path = (self.output_dir / filename).resolve()
        if path.parent != self.output_dir:
            raise GitHubPublisherError("Static output path escapes public directory")
        return path

    def _load_published(self, filename: str) -> Any:
        if not self.base_url:
            return None
        try:
            return self.remote_loader(urljoin(self.base_url, filename))
        except Exception:
            return None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise GitHubPublisherError("Publisher clock and source timestamps must be timezone-aware")
        return value

    def _target(self, session: SessionName, current: datetime) -> datetime:
        if session not in TARGETS:
            raise GitHubPublisherError("Session must be morning or evening")
        local = self._aware(current).astimezone(YANGON)
        return datetime.combine(local.date(), TARGETS[session], tzinfo=YANGON)

    async def _capture_scheduled(
        self,
        session: SessionName,
        *,
        expected_session: SessionName | None = None,
    ) -> tuple[dict[str, str], datetime]:
        if session not in TARGETS:
            raise GitHubPublisherError("Session must be morning or evening")
        if expected_session is not None and session != expected_session:
            raise GitHubPublisherError("Session does not match the scheduled workflow")
        raw_previous = self._load_published("latest.json")
        previous = validate_public_record(raw_previous)
        previous_timestamp = previous["market_datetime"] if previous else None
        if (
            previous_timestamp is None
            and isinstance(raw_previous, dict)
            and raw_previous.get("publication_type") is None
        ):
            legacy_timestamp = raw_previous.get("market_datetime")
            if isinstance(legacy_timestamp, str):
                try:
                    self._aware(datetime.fromisoformat(legacy_timestamp))
                except (ValueError, GitHubPublisherError):
                    pass
                else:
                    previous_timestamp = legacy_timestamp
        previous_instant: datetime | None = None
        if previous_timestamp is not None:
            try:
                previous_instant = self._aware(
                    datetime.fromisoformat(previous_timestamp)
                ).astimezone(timezone.utc)
            except (ValueError, GitHubPublisherError):
                previous_instant = None

        prior_history = validate_history(self._load_published("history.json"))
        prior_session_keys = {
            record_identity(item)
            for item in ([previous] if previous is not None else []) + prior_history
            if item["publication_type"] == "scheduled_result"
        }
        started = self._aware(self.now()).astimezone(YANGON)
        target = self._target(session, started)
        deadline = datetime.combine(
            target.date(), WINDOW_ENDS[session], tzinfo=YANGON
        )
        if started.weekday() >= 5:
            raise GitHubPublisherError("Scheduled publication is allowed Monday through Friday only")
        if started < target:
            self.log(
                {
                    "event": "waiting_for_session_target",
                    "current_yangon": started.isoformat(),
                    "session": session,
                    "session_target_yangon": target.isoformat(),
                    "session_window_end_yangon": deadline.isoformat(),
                }
            )
            await self.sleep((target - started).total_seconds())

        attempt = 0
        while True:
            attempt += 1
            attempt_time = self._aware(self.now()).astimezone(YANGON)
            common_log = {
                "event": "fetch_attempt",
                "attempt": attempt,
                "current_utc": attempt_time.astimezone(timezone.utc).isoformat(),
                "current_yangon": attempt_time.isoformat(),
                "session": session,
                "session_target_yangon": target.isoformat(),
                "session_window_end_yangon": deadline.isoformat(),
                "previous_published_market_datetime": previous_timestamp,
            }
            try:
                sample = await self.client.fetch()
            except Exception as exc:
                self.log(
                    {
                        **common_log,
                        "source_bangkok_timestamp": None,
                        "source_market_datetime": None,
                        "source_yangon_timestamp": None,
                        "accepted": False,
                        "rejection_reason": f"fetch_failed:{type(exc).__name__}",
                        "decision_reason": f"fetch_failed:{type(exc).__name__}",
                    }
                )
            else:
                try:
                    source_time = self._aware(datetime.fromisoformat(sample["marketDateTime"]))
                except (KeyError, TypeError, ValueError) as exc:
                    self.log(
                        {
                            **common_log,
                            "source_bangkok_timestamp": sample.get("marketDateTime")
                            if isinstance(sample, dict)
                            else None,
                            "source_market_datetime": sample.get("marketDateTime")
                            if isinstance(sample, dict)
                            else None,
                            "source_yangon_timestamp": None,
                            "accepted": False,
                            "rejection_reason": "invalid_source_timestamp",
                            "decision_reason": "invalid_source_timestamp",
                        }
                    )
                    raise GitHubPublisherError("SET sample contains an invalid marketDateTime") from exc
                source_bangkok = source_time.astimezone(BANGKOK)
                source_yangon = source_bangkok.astimezone(YANGON)
                captured_at = self._aware(self.now()).astimezone(YANGON)
                source_instant = source_time.astimezone(timezone.utc)
                session_key = (source_yangon.date().isoformat(), TARGETS[session].isoformat())
                rejection_reason: str | None = None
                if previous_instant is not None and source_instant == previous_instant:
                    rejection_reason = "source_timestamp_unchanged"
                elif previous_instant is not None and source_instant < previous_instant:
                    rejection_reason = "source_timestamp_not_newer"
                elif source_yangon.date() != target.date():
                    rejection_reason = "wrong_yangon_date"
                elif source_yangon < target:
                    rejection_reason = "source_before_session_target"
                elif source_yangon > deadline:
                    rejection_reason = "source_after_session_window"
                elif session_key in prior_session_keys:
                    rejection_reason = "result_date_session_already_published"

                self.log(
                    {
                        **common_log,
                        "current_yangon": captured_at.isoformat(),
                        "source_bangkok_timestamp": source_bangkok.isoformat(),
                        "source_market_datetime": sample["marketDateTime"],
                        "source_yangon_timestamp": source_yangon.isoformat(),
                        "accepted": rejection_reason is None,
                        "rejection_reason": rejection_reason,
                        "decision_reason": rejection_reason or "accepted",
                    }
                )
                if rejection_reason is None:
                    return sample, captured_at

            current = self._aware(self.now()).astimezone(YANGON)
            if current >= deadline:
                raise GitHubPublisherError("Collection window expired; published files were not changed")
            await self.sleep(min(POLL_SECONDS, (deadline - current).total_seconds()))

    def write_success_marker(
        self,
        marker_path: str | Path,
        record: dict[str, Any],
    ) -> Path:
        """Write a non-public marker only after production JSON validates."""
        if validate_public_record(record) is None:
            raise GitHubPublisherError("Cannot mark an invalid production record as published")
        latest = validate_public_record(
            json.loads(self._path("latest.json").read_text(encoding="utf-8"))
        )
        history_value = json.loads(self._path("history.json").read_text(encoding="utf-8"))
        history = validate_history(history_value)
        if latest != record or not history or history[0] != record:
            raise GitHubPublisherError("Production files did not pass post-write validation")
        marker = self.clear_success_marker(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(marker.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "production_published": True,
                    "session": record["session"],
                    "market_datetime": record["market_datetime"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(marker)
        return marker

    def clear_success_marker(self, marker_path: str | Path) -> Path:
        """Remove any stale marker before a production collection starts."""
        marker = Path(marker_path).resolve()
        if marker == self.output_dir or marker.is_relative_to(self.output_dir):
            raise GitHubPublisherError("Production success marker must be outside public directory")
        marker.unlink(missing_ok=True)
        return marker

    def _record(
        self,
        sample: dict[str, str],
        session: SessionName,
        captured_at: datetime,
        *,
        publication_type: str,
    ) -> dict[str, Any]:
        calculated = MyanmarTwoDStrategy().calculate(
            last=sample["last"], value=sample["value"]
        )
        captured_yangon = self._aware(captured_at).astimezone(YANGON)
        record = {
            **calculated,
            "market_datetime": sample["marketDateTime"],
            "market_status": sample["marketStatus"],
            "fetched_at": captured_yangon.astimezone(timezone.utc).isoformat(),
            "source_client": sample["sourceClient"],
            "session": session,
            "target_time_yangon": TARGETS[session].isoformat(),
            "captured_at_yangon": captured_yangon.isoformat(),
            "source_market_datetime": sample["marketDateTime"],
            "publication_type": publication_type,
        }
        record["stale"] = (
            is_scheduled_result_stale(record, captured_yangon)
            if publication_type == "scheduled_result"
            else True
        )
        return record

    def _write(self, latest: dict[str, Any], history: list[dict[str, Any]]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        documents = {"latest.json": latest, "history.json": history}
        temporary: dict[str, Path] = {}
        try:
            for filename, document in documents.items():
                path = self._path(filename)
                temp = path.with_suffix(path.suffix + ".tmp")
                temp.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                temporary[filename] = temp
            temporary["history.json"].replace(self._path("history.json"))
            temporary["latest.json"].replace(self._path("latest.json"))
        finally:
            for temp in temporary.values():
                temp.unlink(missing_ok=True)

    async def publish(
        self,
        session: SessionName,
        *,
        expected_session: SessionName | None = None,
    ) -> dict[str, Any]:
        sample, captured_at = await self._capture_scheduled(
            session,
            expected_session=expected_session,
        )
        record = self._record(
            sample,
            session,
            captured_at,
            publication_type="scheduled_result",
        )
        history = validate_history(self._load_published("history.json"))
        refreshed_history = [
            {**item, "stale": is_scheduled_result_stale(item, captured_at)}
            for item in history
        ]
        merged = merge_public_history([record, *refreshed_history])
        self._write(record, merged)
        return record

    async def smoke(
        self,
        session: SessionName,
        *,
        artifact_path: str | Path | None = None,
    ) -> dict[str, Any]:
        if session not in TARGETS:
            raise GitHubPublisherError("Session must be morning or evening")
        sample = await self.client.fetch()
        record = self._record(
            sample,
            session,
            self._aware(self.now()),
            publication_type="smoke_test",
        )
        if artifact_path is not None:
            path = Path(artifact_path).resolve()
            if path.is_relative_to(self.output_dir):
                raise GitHubPublisherError("Smoke artifacts cannot be written under the public directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return record


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", choices=("morning", "evening"), required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--publish-production", action="store_true")
    parser.add_argument("--artifact-path")
    parser.add_argument("--success-marker")
    arguments = parser.parse_args(argv)
    if arguments.once and arguments.success_marker:
        parser.error("--success-marker is available only for production poll mode")
    if arguments.publish_production and arguments.artifact_path:
        parser.error("--artifact-path is available only for --once mode")
    return arguments


async def main() -> None:
    arguments = _arguments()
    session: SessionName = arguments.window
    mode = "once" if arguments.once else "publish_production"
    _json_log(
        {
            "event": "publisher_mode",
            "selected_session": session,
            "mode": mode,
            "github_event_name": os.getenv("GITHUB_EVENT_NAME", ""),
            "github_schedule": os.getenv("GITHUB_SCHEDULE", ""),
        }
    )
    publisher = GitHubPublisher()
    if arguments.once:
        record = await publisher.smoke(session, artifact_path=arguments.artifact_path)
    elif arguments.publish_production:
        expected_session = os.getenv("EXPECTED_SESSION") or None
        success_marker = arguments.success_marker or os.getenv(
            "PRODUCTION_SUCCESS_MARKER", ""
        ).strip()
        if success_marker:
            publisher.clear_success_marker(success_marker)
        record = await publisher.publish(session, expected_session=expected_session)
        if success_marker:
            publisher.write_success_marker(success_marker, record)
    else:  # argparse enforces a mode; retain a defensive non-writing guard.
        raise GitHubPublisherError("An explicit publisher mode is required")
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except GitHubPublisherError as exc:
        print(
            json.dumps(
                {"event": "publisher_failed", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc
