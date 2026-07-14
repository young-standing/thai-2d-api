"""Publish verified Myanmar 2D results as static GitHub Pages JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests

from two_d_service import MyanmarTwoDStrategy
from unified_set_client import UnifiedSetClient

YANGON = ZoneInfo("Asia/Yangon")
SessionName = Literal["morning", "evening"]
TARGETS = {"morning": time(12, 1), "evening": time(16, 30)}
POLL_SECONDS = 30
WINDOW_START_BEFORE_TARGET = timedelta(minutes=3)
WINDOW_END_AFTER_TARGET = timedelta(minutes=2)
MAX_HISTORY = 100
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
TWO_DIGITS = re.compile(r"^\d{2}$")
ONE_DIGIT = re.compile(r"^\d$")
DECIMAL_STRING = re.compile(r"^\d+\.\d+$")
INTEGER_STRING = re.compile(r"^\d+$")


class GitHubPublisherError(RuntimeError):
    pass


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
    if not isinstance(value, dict) or set(value) != set(PUBLIC_FIELDS):
        return None
    record: dict[str, Any] = {}
    for field in PUBLIC_FIELDS:
        item = value[field]
        if field == "stale":
            if not isinstance(item, bool):
                return None
        elif not isinstance(item, str):
            return None
        record[field] = item
    if record["session"] not in {"morning", "evening"}:
        return None
    if record["publication_type"] != "scheduled_result":
        return None
    if record["target_time_yangon"] != TARGETS[record["session"]].isoformat():
        return None
    if record["source_market_datetime"] != record["market_datetime"]:
        return None
    if record["source_client"] not in {"requests", "playwright"}:
        return None
    if record["strategy"] != "set_hundredths_plus_value_million_units":
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
    return record


def validate_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value[:MAX_HISTORY]:
        record = validate_public_record(item)
        if record is None:
            return []
        if record["market_datetime"] in seen:
            continue
        seen.add(record["market_datetime"])
        validated.append(record)
    return validated


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
    ):
        self.client = client or UnifiedSetClient()
        self.output_dir = Path(output_dir).resolve()
        self.remote_loader = remote_loader
        self.now = now or (lambda: datetime.now(YANGON))
        self.sleep = sleep
        self.base_url = base_url if base_url is not None else _default_base_url()

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
        if previous_timestamp is None and isinstance(raw_previous, dict):
            legacy_timestamp = raw_previous.get("market_datetime")
            if isinstance(legacy_timestamp, str):
                try:
                    self._aware(datetime.fromisoformat(legacy_timestamp))
                except (ValueError, GitHubPublisherError):
                    pass
                else:
                    previous_timestamp = legacy_timestamp
        started = self._aware(self.now()).astimezone(YANGON)
        target = self._target(session, started)
        window_start = target - WINDOW_START_BEFORE_TARGET
        deadline = target + WINDOW_END_AFTER_TARGET
        if started.weekday() >= 5:
            raise GitHubPublisherError("Scheduled publication is allowed Monday through Friday only")
        if started < window_start or started > deadline:
            raise GitHubPublisherError("Publisher started outside the scheduled collection window")

        while True:
            try:
                sample = await self.client.fetch()
            except Exception:
                pass
            else:
                try:
                    source_time = self._aware(datetime.fromisoformat(sample["marketDateTime"]))
                except (KeyError, TypeError, ValueError) as exc:
                    raise GitHubPublisherError("SET sample contains an invalid marketDateTime") from exc
                source_yangon = source_time.astimezone(YANGON)
                captured_at = self._aware(self.now()).astimezone(YANGON)
                changed = previous_timestamp is None or sample["marketDateTime"] != previous_timestamp
                expected_date = source_yangon.date() == target.date()
                inside_target_window = target <= source_yangon <= deadline
                captured_inside_window = target <= captured_at <= deadline
                if changed and expected_date and inside_target_window and captured_inside_window:
                    return sample, captured_at

            current = self._aware(self.now()).astimezone(YANGON)
            if current >= deadline:
                raise GitHubPublisherError("Collection window expired; published files were not changed")
            await self.sleep(min(POLL_SECONDS, (deadline - current).total_seconds()))

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
        merged = [record] + [
            item
            for item in refreshed_history
            if item["market_datetime"] != record["market_datetime"]
        ]
        self._write(record, merged[:MAX_HISTORY])
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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", choices=("morning", "evening"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--artifact-path")
    arguments = parser.parse_args()
    if arguments.window is None and not arguments.once:
        parser.error("one of --window or --once is required")
    return arguments


async def main() -> None:
    arguments = _arguments()
    current = datetime.now(YANGON)
    session: SessionName = arguments.window or ("morning" if current.time() < time(14) else "evening")
    publisher = GitHubPublisher()
    if arguments.once:
        record = await publisher.smoke(session, artifact_path=arguments.artifact_path)
    else:
        expected_session = os.getenv("EXPECTED_SESSION") or None
        record = await publisher.publish(session, expected_session=expected_session)
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
