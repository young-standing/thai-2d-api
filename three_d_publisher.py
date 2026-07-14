"""Build and atomically publish static Thai 3D result JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from glo_client import APPROVED_OFFICIAL_HOSTS, GloClient
from three_d_service import STRATEGY, calculate_three_d

THAILAND = ZoneInfo("Asia/Bangkok")
EXPECTED_PUBLICATION_TIME = datetime_time(17, 0)
DEFAULT_PAGES_URL = "https://young-standing.github.io/thai-2d-api"
HISTORY_ALL_NAME = "history-3d-all.json"
PRESERVED_2D_FILES = (
    "latest.json",
    "history.json",
    "history-all.json",
    "history-30-days.json",
)
RECORD_FIELDS = frozenset(
    {
        "draw_date",
        "first_prize",
        "three_d",
        "strategy",
        "source_updated_at",
        "fetched_at",
        "source",
        "source_client",
        "publication_type",
        "stale",
    }
)


class ThreeDPublisherError(RuntimeError):
    """Safe publisher failure."""


def _parse_aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ThreeDPublisherError(f"{field} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ThreeDPublisherError(f"{field} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ThreeDPublisherError(f"{field} must be timezone-aware")
    return parsed


def most_recent_expected_draw(now: datetime) -> date:
    """Return the latest standard 1st/16th draw expected by this date."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ThreeDPublisherError("current time must be timezone-aware")
    local_now = now.astimezone(THAILAND)
    local = local_now.date()
    if local.day > 16 or (
        local.day == 16 and local_now.time().replace(tzinfo=None) >= EXPECTED_PUBLICATION_TIME
    ):
        return local.replace(day=16)
    if local.day > 1 or (
        local.day == 1 and local_now.time().replace(tzinfo=None) >= EXPECTED_PUBLICATION_TIME
    ):
        return local.replace(day=1)
    previous_month_end = local.replace(day=1) - timedelta(days=1)
    return previous_month_end.replace(day=16)


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
        raise ThreeDPublisherError("published 3D record has an unexpected schema")
    try:
        draw_date = date.fromisoformat(record["draw_date"])
    except (TypeError, ValueError) as exc:
        raise ThreeDPublisherError("published draw_date is invalid") from exc
    if draw_date.isoformat() != record["draw_date"]:
        raise ThreeDPublisherError("published draw_date is not canonical")
    calculated = calculate_three_d(record["first_prize"])
    if record["three_d"] != calculated["three_d"] or record["strategy"] != STRATEGY:
        raise ThreeDPublisherError("published 3D calculation does not verify locally")
    if record["source_client"] not in {"http", "playwright"}:
        raise ThreeDPublisherError("published source_client is invalid")
    if record["publication_type"] != "scheduled_result":
        raise ThreeDPublisherError("published record is not a scheduled official result")
    if type(record["stale"]) is not bool:
        raise ThreeDPublisherError("published stale must be boolean")
    parsed_source = urlparse(record["source"] if isinstance(record["source"], str) else "")
    if parsed_source.scheme != "https" or parsed_source.hostname not in APPROVED_OFFICIAL_HOSTS:
        raise ThreeDPublisherError("published source is not an approved official GLO URL")
    _parse_aware(record["fetched_at"], "fetched_at")
    if record["source_updated_at"] is not None:
        _parse_aware(record["source_updated_at"], "source_updated_at")
    return dict(record)


def build_record(sample: dict[str, Any], now: datetime) -> dict[str, Any]:
    calculated = calculate_three_d(sample.get("first_prize"))
    record = {
        "draw_date": sample.get("draw_date"),
        **calculated,
        "source_updated_at": sample.get("source_updated_at"),
        "fetched_at": sample.get("fetched_at"),
        "source": sample.get("source"),
        "source_client": sample.get("source_client"),
        "publication_type": "scheduled_result",
        "stale": date.fromisoformat(sample["draw_date"]) < most_recent_expected_draw(now),
    }
    return validate_record(record)


class PublishedHistoryClient:
    """Read only the trusted Pages all-time 3D history without redirects."""

    def __init__(
        self,
        base_url: str = DEFAULT_PAGES_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()

    def _url(self) -> str:
        url = f"{self.base_url}/{HISTORY_ALL_NAME}"
        parsed = urlparse(url)
        expected = urlparse(DEFAULT_PAGES_URL)
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected.hostname
            or parsed.path != f"{expected.path}/{HISTORY_ALL_NAME}"
            or parsed.query
            or parsed.fragment
        ):
            raise ThreeDPublisherError("published history URL is not the trusted Pages path")
        return url

    def load(self) -> list[dict[str, Any]]:
        try:
            response = self.session.get(self._url(), timeout=15, allow_redirects=False)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise ThreeDPublisherError("could not download published 3D history") from exc
        if response.status_code == 404:
            return []
        if 300 <= response.status_code < 400:
            raise ThreeDPublisherError("untrusted published-history redirect rejected")
        if response.status_code != 200:
            raise ThreeDPublisherError(
                f"published 3D history returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise ThreeDPublisherError("published 3D history is not valid JSON") from exc
        if not isinstance(payload, list):
            raise ThreeDPublisherError("published 3D history must be a list")
        return [validate_record(item) for item in payload]


def preserve_published_2d_files(
    output_dir: Path,
    *,
    base_url: str = DEFAULT_PAGES_URL,
    session: requests.Session | None = None,
) -> None:
    """Copy current live 2D JSON into a temporary full-site artifact.

    This does not interpret or alter 2D data. It prevents a Pages deployment,
    which replaces the complete site, from reverting live 2D files to checkout
    placeholders while the independent 3D pipeline is publishing.
    """
    client = session or requests.Session()
    expected = urlparse(DEFAULT_PAGES_URL)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in PRESERVED_2D_FILES:
        url = f"{base_url.rstrip('/')}/{name}"
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected.hostname
            or parsed.path != f"{expected.path}/{name}"
        ):
            raise ThreeDPublisherError("2D preservation URL is not the trusted Pages path")
        try:
            response = client.get(url, timeout=15, allow_redirects=False)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise ThreeDPublisherError("could not preserve current published 2D files") from exc
        if response.status_code == 404:
            continue
        if response.status_code != 200:
            raise ThreeDPublisherError(
                f"published 2D preservation returned HTTP {response.status_code}"
            )
        if len(response.content) > 5_000_000:
            raise ThreeDPublisherError("published 2D file exceeded the safe size limit")
        try:
            json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ThreeDPublisherError("published 2D preservation file was invalid JSON") from exc
        temporary = output_dir / f".{name}.tmp"
        temporary.write_bytes(response.content)
        temporary.replace(output_dir / name)


def _record_timestamp(record: dict[str, Any]) -> datetime:
    return _parse_aware(record["source_updated_at"] or record["fetched_at"], "record timestamp")


def merge_history(
    existing: list[dict[str, Any]], new_record: dict[str, Any]
) -> list[dict[str, Any]]:
    valid_new = validate_record(new_record)
    by_date: dict[str, dict[str, Any]] = {}
    for item in existing:
        valid = validate_record(item)
        current = by_date.get(valid["draw_date"])
        if current is None or _record_timestamp(valid) > _record_timestamp(current):
            by_date[valid["draw_date"]] = valid
    old = by_date.get(valid_new["draw_date"])
    if old is None or _record_timestamp(valid_new) > _record_timestamp(old):
        by_date[valid_new["draw_date"]] = valid_new
    return sorted(by_date.values(), key=lambda item: item["draw_date"], reverse=True)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def write_outputs_atomic(output_dir: Path, all_history: list[dict[str, Any]]) -> None:
    """Validate and replace the three production files as one rollback unit."""
    if not all_history:
        raise ThreeDPublisherError("refusing to publish empty 3D history")
    validated = [validate_record(item) for item in all_history]
    payloads = {
        "latest-3d.json": validated[0],
        "history-3d.json": validated[:50],
        HISTORY_ALL_NAME: validated,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="three-d-stage-", dir=output_dir))
    backups = Path(tempfile.mkdtemp(prefix="three-d-backup-", dir=output_dir))
    replaced: list[str] = []
    try:
        for name, payload in payloads.items():
            staged = staging / name
            staged.write_bytes(_json_bytes(payload))
            decoded = json.loads(staged.read_text(encoding="utf-8"))
            if name == "latest-3d.json":
                validate_record(decoded)
            else:
                if not isinstance(decoded, list):
                    raise ThreeDPublisherError(f"{name} did not validate as a list")
                for item in decoded:
                    validate_record(item)
        for name in payloads:
            target = output_dir / name
            if target.exists():
                shutil.copy2(target, backups / name)
            (staging / name).replace(target)
            replaced.append(name)
    except Exception as exc:
        for name in reversed(replaced):
            target = output_dir / name
            backup = backups / name
            if backup.exists():
                shutil.copy2(backup, target)
            elif target.exists():
                target.unlink()
        if isinstance(exc, ThreeDPublisherError):
            raise
        raise ThreeDPublisherError("atomic 3D output failed; previous files restored") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backups, ignore_errors=True)


class ThreeDPublisher:
    def __init__(
        self,
        *,
        client: Any | None = None,
        history_loader: Callable[[], list[dict[str, Any]]] | None = None,
        output_dir: Path | str = "public",
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client or GloClient()
        self.history_loader = history_loader or PublishedHistoryClient().load
        self.output_dir = Path(output_dir)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep

    async def smoke(self) -> dict[str, Any]:
        sample = await self.client.fetch()
        return build_record(sample, self.now())

    async def publish(
        self,
        *,
        expected_draw_date: str | None = None,
        poll: bool = False,
        interval_seconds: int = 60,
        window_seconds: int = 1800,
    ) -> dict[str, Any]:
        expected = (
            date.fromisoformat(expected_draw_date)
            if expected_draw_date
            else most_recent_expected_draw(self.now())
        )
        deadline = self.now() + timedelta(seconds=window_seconds)
        last_error: BaseException | None = None
        while True:
            try:
                sample = await self.client.fetch()
                record = build_record(sample, self.now())
                if record["draw_date"] != expected.isoformat():
                    raise ThreeDPublisherError(
                        f"official result is for {record['draw_date']}, expected {expected.isoformat()}"
                    )
                existing = await asyncio.to_thread(self.history_loader)
                merged = merge_history(existing, record)
                write_outputs_atomic(self.output_dir, merged)
                return merged[0]
            except Exception as exc:
                last_error = exc
            if not poll or self.now() + timedelta(seconds=interval_seconds) > deadline:
                raise ThreeDPublisherError(
                    f"valid official result for {expected.isoformat()} was not available"
                ) from last_error
            await self.sleep(interval_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="smoke fetch only; never writes")
    mode.add_argument("--publish", action="store_true", help="write validated production JSON")
    parser.add_argument("--poll", action="store_true")
    parser.add_argument("--expected-draw-date")
    parser.add_argument("--output-dir", default="public")
    parser.add_argument(
        "--preserve-current-pages",
        action="store_true",
        help="hydrate live 2D JSON into a temporary full-site Pages artifact",
    )
    return parser


async def main() -> None:
    args = _parser().parse_args()
    publisher = ThreeDPublisher(output_dir=args.output_dir)
    if args.once:
        result = await publisher.smoke()
    else:
        if args.preserve_current_pages:
            await asyncio.to_thread(preserve_published_2d_files, Path(args.output_dir))
        result = await publisher.publish(
            expected_draw_date=args.expected_draw_date or None,
            poll=args.poll,
            interval_seconds=int(os.getenv("THREE_D_POLL_INTERVAL_SECONDS", "60")),
            window_seconds=int(os.getenv("THREE_D_POLL_WINDOW_SECONDS", "1800")),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ThreeDPublisherError, ValueError) as exc:
        raise SystemExit(f"three_d_publish_error: {exc}") from exc
