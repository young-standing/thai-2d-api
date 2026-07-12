"""Scheduled SET collector for the two Myanmar 2D result windows."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from market_repository import MarketRepository
from unified_set_client import UnifiedSetClient

YANGON = ZoneInfo("Asia/Yangon")
WindowName = Literal["morning", "evening"]


def _parse_time(value: str, variable: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{variable} must be HH:MM or HH:MM:SS") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{variable} must not include a timezone")
    return parsed


@dataclass(frozen=True)
class CollectorConfig:
    morning_target: time = time(12, 1)
    evening_target: time = time(16, 30)
    fetch_interval_seconds: float = 30
    morning_window_start: time = time(11, 59, 30)
    morning_window_end: time = time(12, 2)
    evening_window_start: time = time(16, 28, 30)
    evening_window_end: time = time(16, 32)
    weekdays_only: bool = True

    @classmethod
    def from_environment(cls) -> "CollectorConfig":
        interval = float(os.getenv("FETCH_INTERVAL_SECONDS", "30"))
        if interval <= 0:
            raise ValueError("FETCH_INTERVAL_SECONDS must be greater than zero")
        return cls(
            morning_target=_parse_time(os.getenv("MORNING_TARGET", "12:01"), "MORNING_TARGET"),
            evening_target=_parse_time(os.getenv("EVENING_TARGET", "16:30"), "EVENING_TARGET"),
            fetch_interval_seconds=interval,
            morning_window_start=_parse_time(
                os.getenv("MORNING_WINDOW_START", "11:59:30"), "MORNING_WINDOW_START"
            ),
            morning_window_end=_parse_time(
                os.getenv("MORNING_WINDOW_END", "12:02:00"), "MORNING_WINDOW_END"
            ),
            evening_window_start=_parse_time(
                os.getenv("EVENING_WINDOW_START", "16:28:30"), "EVENING_WINDOW_START"
            ),
            evening_window_end=_parse_time(
                os.getenv("EVENING_WINDOW_END", "16:32:00"), "EVENING_WINDOW_END"
            ),
        )


@dataclass(frozen=True)
class CollectionWindow:
    name: WindowName
    target: datetime
    start: datetime
    end: datetime


class StructuredLogger:
    def __init__(self, logger: logging.Logger | None = None):
        self.logger = logger or logging.getLogger("scheduled_collector")

    def event(self, event: str, **fields: object) -> None:
        self.logger.info(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


class ProcessLock:
    """Small cross-process lock based on exclusive file creation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, str(os.getpid()).encode("ascii"))
        except FileExistsError as exc:
            raise RuntimeError(f"Another scheduled collector is already running ({self.path})") from exc

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            with suppress(FileNotFoundError):
                self.path.unlink()


class ScheduledCollector:
    def __init__(
        self,
        client: UnifiedSetClient,
        repository: MarketRepository,
        config: CollectorConfig | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: StructuredLogger | None = None,
    ):
        self.client = client
        self.repository = repository
        self.config = config or CollectorConfig.from_environment()
        self._now = now or (lambda: datetime.now(YANGON))
        self._sleep = sleep
        self.log = logger or StructuredLogger()
        self._window_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()

    @staticmethod
    def _yangon_datetime(day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, tzinfo=YANGON)

    def windows_for_date(self, day: date) -> tuple[CollectionWindow, CollectionWindow]:
        return (
            CollectionWindow(
                "morning",
                self._yangon_datetime(day, self.config.morning_target),
                self._yangon_datetime(day, self.config.morning_window_start),
                self._yangon_datetime(day, self.config.morning_window_end),
            ),
            CollectionWindow(
                "evening",
                self._yangon_datetime(day, self.config.evening_target),
                self._yangon_datetime(day, self.config.evening_window_start),
                self._yangon_datetime(day, self.config.evening_window_end),
            ),
        )

    def current_window(self, now: datetime | None = None) -> CollectionWindow | None:
        current = self._require_aware(now or self._now()).astimezone(YANGON)
        if self.config.weekdays_only and current.weekday() >= 5:
            return None
        for window in self.windows_for_date(current.date()):
            if window.start <= current <= window.end:
                return window
        return None

    def next_window(self, now: datetime | None = None) -> CollectionWindow:
        current = self._require_aware(now or self._now()).astimezone(YANGON)
        for offset in range(8):
            day = current.date() + timedelta(days=offset)
            if self.config.weekdays_only and day.weekday() >= 5:
                continue
            for window in self.windows_for_date(day):
                if window.end >= current:
                    return window
        raise RuntimeError("Could not calculate the next collection window")

    @staticmethod
    def _require_aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("All scheduler datetimes must be timezone-aware")
        return value

    @staticmethod
    def _parse_source_datetime(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("marketDateTime must be a valid ISO-8601 datetime") from exc
        return ScheduledCollector._require_aware(parsed)

    async def run_window(self, window: CollectionWindow) -> bool:
        if self._window_lock.locked():
            raise RuntimeError("A collection window is already running")
        async with self._window_lock:
            latest = self.repository.get_latest()
            previous_timestamp = latest["market_datetime"] if latest else None
            self.log.event("window_started", window=window.name, start=window.start, end=window.end)

            while not self.stop_event.is_set():
                current = self._require_aware(self._now()).astimezone(YANGON)
                if current > window.end:
                    self.log.event("window_expired", window=window.name)
                    return False

                try:
                    sample = await self.client.fetch()
                    source_datetime = self._parse_source_datetime(sample["marketDateTime"])
                    save_result = self.repository.save_sample(sample)
                    self.log.event(
                        "fetch_success",
                        window=window.name,
                        market_datetime=sample["marketDateTime"],
                        history_inserted=save_result["history_inserted"],
                    )
                except Exception as exc:
                    self.log.event("fetch_failed", window=window.name, error_type=type(exc).__name__)
                else:
                    timestamp_changed = sample["marketDateTime"] != previous_timestamp
                    source_after_target = source_datetime.astimezone(YANGON) >= window.target
                    if timestamp_changed and source_after_target:
                        self.log.event(
                            "result_captured",
                            window=window.name,
                            market_datetime=sample["marketDateTime"],
                        )
                        return True
                    self.log.event(
                        "source_timestamp_unchanged",
                        window=window.name,
                        market_datetime=sample["marketDateTime"],
                    )
                    previous_timestamp = sample["marketDateTime"]

                remaining = (window.end - current).total_seconds()
                if remaining <= 0:
                    self.log.event("window_expired", window=window.name)
                    return False
                await self._sleep(min(self.config.fetch_interval_seconds, remaining))

            return False

    async def run_forever(self, *, once: bool = False) -> None:
        search_from: datetime | None = None
        while not self.stop_event.is_set():
            current = self._require_aware(self._now()).astimezone(YANGON)
            if search_from is None:
                window = self.current_window(current) or self.next_window(current)
            else:
                window = self.next_window(search_from)
            if current < window.start:
                delay = (window.start - current).total_seconds()
                self.log.event("next_window", window=window.name, starts_at=window.start)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass
            await self.run_window(window)
            if once:
                return
            # Mark the entire window consumed even when result capture stopped
            # early, so it cannot be reopened by the outer scheduler loop.
            search_from = window.end + timedelta(microseconds=1)

    async def run_now(self, name: WindowName) -> bool:
        """Run the named configured window immediately for operational debugging."""
        now = self._require_aware(self._now()).astimezone(YANGON)
        configured = self.windows_for_date(now.date())[0 if name == "morning" else 1]
        duration = configured.end - configured.start
        forced = CollectionWindow(name, now, now, now + duration)
        return await self.run_window(forced)

    def request_stop(self) -> None:
        self.stop_event.set()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--run-morning-now", action="store_true")
    modes.add_argument("--run-evening-now", action="store_true")
    modes.add_argument("--once", action="store_true", help="Run the next/current scheduled window once")
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> None:
    repository = MarketRepository(os.getenv("DATABASE_PATH", "thai_2d.sqlite3"))
    repository.initialize()
    collector = ScheduledCollector(UnifiedSetClient(), repository)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, collector.request_stop)
        except NotImplementedError:
            signal.signal(
                signal_name,
                lambda *_args: loop.call_soon_threadsafe(collector.request_stop),
            )

    if args.run_morning_now:
        await collector.run_now("morning")
    elif args.run_evening_now:
        await collector.run_now("evening")
    else:
        await collector.run_forever(once=args.once)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    lock = ProcessLock(os.getenv("COLLECTOR_LOCK_FILE", ".scheduled_collector.lock"))
    lock.acquire()
    try:
        asyncio.run(_async_main(_arguments()))
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()


if __name__ == "__main__":
    main()
