from datetime import datetime, timedelta, timezone

import pytest

from scheduled_collector import (
    YANGON,
    CollectorConfig,
    ScheduledCollector,
)


class FakeClock:
    def __init__(self, current):
        self.current = current
        self.sleeps = []

    def now(self):
        return self.current

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def fetch(self):
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)


class FakeRepository:
    def __init__(self, latest=None):
        self.latest = dict(latest) if latest else None
        self.saved = []

    def get_latest(self):
        return dict(self.latest) if self.latest else None

    def save_sample(self, sample):
        self.saved.append(dict(sample))
        self.latest = {"market_datetime": sample["marketDateTime"]}
        return {"latest_updated": True, "history_inserted": True}


class FakeLogger:
    def __init__(self):
        self.events = []

    def event(self, event, **fields):
        self.events.append((event, fields))


def market_sample(timestamp):
    return {
        "last": "1621.550000",
        "value": "77145337740",
        "marketDateTime": timestamp,
        "marketStatus": "Closed",
        "change": "13.250000",
        "percentChange": "0.820000",
        "sourceClient": "playwright",
    }


def collector_at(current, outcomes=None, latest=None):
    clock = FakeClock(current)
    logger = FakeLogger()
    collector = ScheduledCollector(
        FakeClient(outcomes or [market_sample("2026-07-13T12:01:00+06:30")]),
        FakeRepository(latest),
        CollectorConfig(),
        now=clock.now,
        sleep=clock.sleep,
        logger=logger,
    )
    return collector, clock, logger


def test_before_morning_window_selects_morning():
    collector, _, _ = collector_at(datetime(2026, 7, 13, 10, 0, tzinfo=YANGON))
    assert collector.current_window() is None
    assert collector.next_window().name == "morning"
    assert collector.next_window().start.hour == 11
    assert collector.next_window().start.minute == 59
    assert collector.next_window().start.second == 30


def test_inside_morning_window():
    collector, _, _ = collector_at(datetime(2026, 7, 13, 12, 0, tzinfo=YANGON))
    assert collector.current_window().name == "morning"


def test_after_morning_window_selects_evening():
    collector, _, _ = collector_at(datetime(2026, 7, 13, 12, 3, tzinfo=YANGON))
    assert collector.current_window() is None
    assert collector.next_window().name == "evening"


def test_inside_evening_window():
    collector, _, _ = collector_at(datetime(2026, 7, 13, 16, 30, tzinfo=YANGON))
    assert collector.current_window().name == "evening"


def test_weekend_skips_to_monday():
    collector, _, _ = collector_at(datetime(2026, 7, 11, 12, 0, tzinfo=YANGON))
    assert collector.current_window() is None
    next_window = collector.next_window()
    assert next_window.name == "morning"
    assert next_window.start.date().isoformat() == "2026-07-13"


@pytest.mark.asyncio
async def test_new_source_timestamp_after_target_is_captured():
    old = {"market_datetime": "2026-07-13T11:59:00+06:30"}
    new = market_sample("2026-07-13T12:01:05+06:30")
    collector, _, logger = collector_at(
        datetime(2026, 7, 13, 12, 1, 5, tzinfo=YANGON), [new], old
    )
    result = await collector.run_window(collector.current_window())
    assert result is True
    assert collector.client.calls == 1
    assert "result_captured" in [event for event, _ in logger.events]


@pytest.mark.asyncio
async def test_unchanged_source_timestamp_retries():
    timestamp = "2026-07-13T12:01:00+06:30"
    collector, clock, logger = collector_at(
        datetime(2026, 7, 13, 12, 1, 0, tzinfo=YANGON),
        [market_sample(timestamp)],
        {"market_datetime": timestamp},
    )
    await collector.run_window(collector.current_window())
    assert collector.client.calls > 1
    assert clock.sleeps
    assert "source_timestamp_unchanged" in [event for event, _ in logger.events]


@pytest.mark.asyncio
async def test_window_expiration_logs_and_returns_false():
    timestamp = "2026-07-13T11:58:00+06:30"
    collector, _, logger = collector_at(
        datetime(2026, 7, 13, 12, 1, 45, tzinfo=YANGON),
        [market_sample(timestamp)],
        {"market_datetime": timestamp},
    )
    result = await collector.run_window(collector.current_window())
    assert result is False
    assert "window_expired" in [event for event, _ in logger.events]


@pytest.mark.asyncio
async def test_fetch_failure_is_retried_then_captured():
    outcomes = [RuntimeError("temporary"), market_sample("2026-07-13T12:01:30+06:30")]
    collector, _, logger = collector_at(
        datetime(2026, 7, 13, 12, 1, 0, tzinfo=YANGON), outcomes
    )
    result = await collector.run_window(collector.current_window())
    assert result is True
    assert collector.client.calls == 2
    assert "fetch_failed" in [event for event, _ in logger.events]


@pytest.mark.asyncio
async def test_early_stop_after_result_capture_does_not_sleep():
    collector, clock, _ = collector_at(
        datetime(2026, 7, 13, 16, 30, 1, tzinfo=YANGON),
        [market_sample("2026-07-13T17:00:01+07:00")],
        {"market_datetime": "2026-07-13T16:59:00+07:00"},
    )
    assert await collector.run_window(collector.current_window()) is True
    assert clock.sleeps == []


def test_timezone_correctness_for_schedule_and_source_conversion():
    utc_time = datetime(2026, 7, 13, 5, 30, tzinfo=timezone.utc)
    collector, _, _ = collector_at(utc_time)
    window = collector.current_window(utc_time)
    assert window.name == "morning"
    assert window.start.tzinfo == YANGON
    source = collector._parse_source_datetime("2026-07-13T12:31:00+07:00")
    assert source.astimezone(YANGON) == window.target


def test_naive_datetime_is_rejected():
    collector, _, _ = collector_at(datetime(2026, 7, 13, 10, 0, tzinfo=YANGON))
    with pytest.raises(ValueError, match="timezone-aware"):
        collector.current_window(datetime(2026, 7, 13, 12, 0))


def test_next_window_after_consumed_morning_is_evening():
    collector, _, _ = collector_at(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    morning = collector.current_window()
    next_window = collector.next_window(morning.end + timedelta(microseconds=1))
    assert next_window.name == "evening"
