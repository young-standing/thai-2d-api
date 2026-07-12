"""Publish verified Myanmar 2D results as static GitHub Pages JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, time, timedelta, timezone
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
WINDOW_SECONDS = 300
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
    except ValueError:
        return None
    if market_time.tzinfo is None or fetched_time.tzinfo is None:
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
        local = self._aware(current).astimezone(YANGON)
        return datetime.combine(local.date(), TARGETS[session], tzinfo=YANGON)

    async def _capture(self, session: SessionName, *, poll: bool) -> dict[str, str]:
        previous = validate_public_record(self._load_published("latest.json"))
        previous_timestamp = previous["market_datetime"] if previous else None
        started = self._aware(self.now()).astimezone(YANGON)
        deadline = started + timedelta(seconds=WINDOW_SECONDS)
        target = self._target(session, started)

        while True:
            try:
                sample = await self.client.fetch()
            except Exception as exc:
                if not poll or self._aware(self.now()).astimezone(YANGON) >= deadline:
                    raise GitHubPublisherError("SET fetch failed; published files were not changed") from exc
            else:
                try:
                    source_time = self._aware(datetime.fromisoformat(sample["marketDateTime"]))
                except (KeyError, ValueError) as exc:
                    raise GitHubPublisherError("SET sample contains an invalid marketDateTime") from exc
                changed = sample["marketDateTime"] != previous_timestamp
                after_target = source_time.astimezone(YANGON) >= target
                if not poll or (changed and after_target):
                    return sample

            current = self._aware(self.now()).astimezone(YANGON)
            if current >= deadline:
                raise GitHubPublisherError("Collection window expired; published files were not changed")
            await self.sleep(min(POLL_SECONDS, (deadline - current).total_seconds()))

    def _record(self, sample: dict[str, str], session: SessionName) -> dict[str, Any]:
        calculated = MyanmarTwoDStrategy().calculate(
            last=sample["last"], value=sample["value"]
        )
        return {
            **calculated,
            "market_datetime": sample["marketDateTime"],
            "market_status": sample["marketStatus"],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source_client": sample["sourceClient"],
            "session": session,
            "stale": False,
        }

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

    async def publish(self, session: SessionName, *, poll: bool = True) -> dict[str, Any]:
        sample = await self._capture(session, poll=poll)
        record = self._record(sample, session)
        history = validate_history(self._load_published("history.json"))
        merged = [record] + [
            item for item in history if item["market_datetime"] != record["market_datetime"]
        ]
        self._write(record, merged[:MAX_HISTORY])
        return record


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--window", choices=("morning", "evening"))
    modes.add_argument("--once", action="store_true")
    return parser.parse_args()


async def main() -> None:
    arguments = _arguments()
    current = datetime.now(YANGON)
    session: SessionName = arguments.window or ("morning" if current.time() < time(14) else "evening")
    record = await GitHubPublisher().publish(session, poll=not arguments.once)
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
