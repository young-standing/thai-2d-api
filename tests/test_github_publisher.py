import json
from datetime import datetime, timedelta

import pytest

from github_publisher import (
    PUBLIC_FIELDS,
    GitHubPublisher,
    GitHubPublisherError,
    YANGON,
    expected_scheduled_result,
    is_scheduled_result_stale,
)


MORNING_SOURCE = "2026-07-13T12:31:00+07:00"
EVENING_SOURCE = "2026-07-13T17:00:00+07:00"


def sample(
    market_datetime=MORNING_SOURCE,
    *,
    last="1621.550000",
    value="77145337740",
    source="playwright",
):
    return {
        "last": last,
        "value": value,
        "marketDateTime": market_datetime,
        "marketStatus": "Closed",
        "change": "13.250000",
        "percentChange": "0.820000",
        "sourceClient": source,
    }


def public_record(market_datetime, *, session="morning", number="55", stale=False):
    target = "12:01:00" if session == "morning" else "16:30:00"
    return {
        "number": number,
        "index_digit": number[0],
        "value_digit": number[1],
        "set_index": "1621.550000",
        "value_raw": "77145337740",
        "value_million": "77145.337740",
        "market_datetime": market_datetime,
        "market_status": "Closed",
        "fetched_at": "2026-07-13T05:31:05+00:00",
        "source_client": "playwright",
        "strategy": "set_hundredths_plus_value_million_units",
        "session": session,
        "target_time_yangon": target,
        "captured_at_yangon": "2026-07-13T12:01:05+06:30",
        "source_market_datetime": market_datetime,
        "publication_type": "scheduled_result",
        "stale": stale,
    }


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


class FakeClock:
    def __init__(self, current):
        self.current = current

    def now(self):
        return self.current

    async def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def loader(latest=None, history=None):
    def load(url):
        if url.endswith("latest.json"):
            return latest
        if url.endswith("history.json"):
            return history
        raise AssertionError(f"unexpected URL: {url}")

    return load


def publisher(tmp_path, client, clock, *, latest=None, history=None):
    return GitHubPublisher(
        client,
        output_dir=tmp_path / "public",
        remote_loader=loader(latest, history),
        now=clock.now,
        sleep=clock.sleep,
        base_url="https://young-standing.github.io/thai-2d-api/",
    )


@pytest.mark.asyncio
async def test_evening_capture_exactly_at_target(tmp_path):
    prior = public_record(MORNING_SOURCE)
    clock = FakeClock(datetime(2026, 7, 13, 16, 30, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample(EVENING_SOURCE)]), clock, latest=prior, history=[prior]
    ).publish("evening", expected_session="evening")
    assert result["market_datetime"] == EVENING_SOURCE
    assert result["target_time_yangon"] == "16:30:00"
    assert result["publication_type"] == "scheduled_result"
    assert result["stale"] is False


@pytest.mark.asyncio
async def test_evening_capture_after_target(tmp_path):
    prior = public_record(MORNING_SOURCE)
    after_target = "2026-07-13T17:00:30+07:00"
    clock = FakeClock(datetime(2026, 7, 13, 16, 30, 30, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample(after_target)]), clock, latest=prior, history=[prior]
    ).publish("evening")
    assert result["market_datetime"] == after_target


@pytest.mark.asyncio
async def test_pre_target_timestamp_is_rejected_then_target_is_captured(tmp_path):
    prior = public_record(MORNING_SOURCE)
    before_target = sample("2026-07-13T16:59:59+07:00")
    client = FakeClient([before_target, sample(EVENING_SOURCE)])
    clock = FakeClock(datetime(2026, 7, 13, 16, 29, 30, tzinfo=YANGON))
    result = await publisher(tmp_path, client, clock, latest=prior).publish("evening")
    assert client.calls == 2
    assert result["source_market_datetime"] == EVENING_SOURCE


@pytest.mark.asyncio
async def test_wrong_day_timestamp_is_rejected(tmp_path):
    prior = public_record(MORNING_SOURCE)
    wrong_day = sample("2026-07-12T17:00:00+07:00")
    clock = FakeClock(datetime(2026, 7, 13, 16, 31, 30, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([wrong_day]), clock, latest=prior)
    with pytest.raises(GitHubPublisherError, match="expired"):
        await instance.publish("evening")
    assert not (tmp_path / "public/latest.json").exists()


@pytest.mark.asyncio
async def test_unchanged_timestamp_is_rejected(tmp_path):
    prior = public_record(EVENING_SOURCE, session="evening")
    clock = FakeClock(datetime(2026, 7, 13, 16, 31, 30, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([sample(EVENING_SOURCE)]), clock, latest=prior)
    with pytest.raises(GitHubPublisherError, match="expired"):
        await instance.publish("evening")
    assert not (tmp_path / "public/latest.json").exists()


@pytest.mark.asyncio
async def test_manual_once_does_not_modify_public_files(tmp_path):
    output = tmp_path / "public"
    output.mkdir()
    (output / "latest.json").write_text('{"valid":"latest"}\n')
    (output / "history.json").write_text('[{"valid":"history"}]\n')
    clock = FakeClock(datetime(2026, 7, 13, 14, 0, tzinfo=YANGON))
    result = await publisher(tmp_path, FakeClient([sample()]), clock).smoke("morning")
    assert result["publication_type"] == "smoke_test"
    assert result["stale"] is True
    assert (output / "latest.json").read_text() == '{"valid":"latest"}\n'
    assert (output / "history.json").read_text() == '[{"valid":"history"}]\n'


@pytest.mark.asyncio
async def test_manual_once_can_write_non_public_artifact(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 14, 0, tzinfo=YANGON))
    artifact = tmp_path / "artifacts/smoke.json"
    await publisher(tmp_path, FakeClient([sample()]), clock).smoke(
        "morning", artifact_path=artifact
    )
    assert json.loads(artifact.read_text())["publication_type"] == "smoke_test"
    assert not (tmp_path / "public/latest.json").exists()


@pytest.mark.asyncio
async def test_failed_scheduled_collection_preserves_prior_files(tmp_path):
    output = tmp_path / "public"
    output.mkdir()
    (output / "latest.json").write_text('{"valid":"latest"}\n')
    (output / "history.json").write_text('[{"valid":"history"}]\n')
    clock = FakeClock(datetime(2026, 7, 13, 16, 31, 30, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([RuntimeError("failed")]), clock)
    with pytest.raises(GitHubPublisherError, match="not changed"):
        await instance.publish("evening")
    assert (output / "latest.json").read_text() == '{"valid":"latest"}\n'
    assert (output / "history.json").read_text() == '[{"valid":"history"}]\n'


@pytest.mark.asyncio
async def test_scheduled_success_updates_latest_and_history(tmp_path):
    prior = public_record("2026-07-10T17:00:00+07:00", session="evening")
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample()]), clock, latest=prior, history=[prior]
    ).publish("morning")
    latest = json.loads((tmp_path / "public/latest.json").read_text())
    history = json.loads((tmp_path / "public/history.json").read_text())
    assert latest == result
    assert [item["market_datetime"] for item in history] == [
        MORNING_SOURCE,
        prior["market_datetime"],
    ]


@pytest.mark.asyncio
async def test_morning_and_evening_remain_distinct_in_history(tmp_path):
    morning = public_record(MORNING_SOURCE)
    clock = FakeClock(datetime(2026, 7, 13, 16, 30, tzinfo=YANGON))
    await publisher(
        tmp_path,
        FakeClient([sample(EVENING_SOURCE)]),
        clock,
        latest=morning,
        history=[morning],
    ).publish("evening")
    history = json.loads((tmp_path / "public/history.json").read_text())
    assert [(item["session"], item["market_datetime"]) for item in history] == [
        ("evening", EVENING_SOURCE),
        ("morning", MORNING_SOURCE),
    ]


def test_stale_calculation_around_session_and_weekend_boundaries():
    friday_evening = public_record("2026-07-10T17:00:00+07:00", session="evening")
    monday_morning = public_record(MORNING_SOURCE, session="morning")
    monday_evening = public_record(EVENING_SOURCE, session="evening")

    assert not is_scheduled_result_stale(
        friday_evening, datetime(2026, 7, 11, 18, 0, tzinfo=YANGON)
    )
    assert not is_scheduled_result_stale(
        friday_evening, datetime(2026, 7, 13, 12, 0, 59, tzinfo=YANGON)
    )
    assert is_scheduled_result_stale(
        friday_evening, datetime(2026, 7, 13, 12, 1, tzinfo=YANGON)
    )
    assert not is_scheduled_result_stale(
        monday_morning, datetime(2026, 7, 13, 16, 29, 59, tzinfo=YANGON)
    )
    assert is_scheduled_result_stale(
        monday_morning, datetime(2026, 7, 13, 16, 30, tzinfo=YANGON)
    )
    assert not is_scheduled_result_stale(
        monday_evening, datetime(2026, 7, 13, 16, 30, tzinfo=YANGON)
    )


def test_expected_result_before_morning_and_on_weekend():
    assert expected_scheduled_result(
        datetime(2026, 7, 13, 11, 0, tzinfo=YANGON)
    ) == (datetime(2026, 7, 10).date(), "evening")
    assert expected_scheduled_result(
        datetime(2026, 7, 12, 18, 0, tzinfo=YANGON)
    ) == (datetime(2026, 7, 10).date(), "evening")


@pytest.mark.asyncio
async def test_session_must_match_scheduled_workflow(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    client = FakeClient([sample()])
    with pytest.raises(GitHubPublisherError, match="does not match"):
        await publisher(tmp_path, client, clock).publish(
            "morning", expected_session="evening"
        )
    assert client.calls == 0


@pytest.mark.asyncio
async def test_weekend_scheduled_publication_is_rejected(tmp_path):
    clock = FakeClock(datetime(2026, 7, 11, 12, 1, tzinfo=YANGON))
    client = FakeClient([sample("2026-07-11T12:31:00+07:00")])
    with pytest.raises(GitHubPublisherError, match="Monday through Friday"):
        await publisher(tmp_path, client, clock).publish("morning")
    assert client.calls == 0


@pytest.mark.asyncio
async def test_leading_zero_result_and_metadata(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path,
        FakeClient([sample(last="10.00", value="5000000")]),
        clock,
        history=[],
    ).publish("morning")
    assert result["number"] == "05"
    assert result["source_market_datetime"] == MORNING_SOURCE
    assert result["captured_at_yangon"].endswith("+06:30")


@pytest.mark.asyncio
async def test_history_is_newest_first_and_limited_to_100(tmp_path):
    base = datetime(2026, 1, 1, 9, 0, tzinfo=YANGON)
    history = [
        public_record((base + timedelta(minutes=index)).isoformat())
        for index in range(100)
    ]
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample()]), clock, history=history
    ).publish("morning")
    saved = json.loads((tmp_path / "public/history.json").read_text())
    assert len(saved) == 100
    assert saved[0]["market_datetime"] == result["market_datetime"]


def test_safe_static_output_paths(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([sample()]), clock)
    with pytest.raises(GitHubPublisherError, match="Unsafe"):
        instance._path("../secret.json")


@pytest.mark.asyncio
async def test_published_output_contains_only_whitelisted_fields_and_no_secrets(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    await publisher(tmp_path, FakeClient([sample()]), clock, history=[]).publish("morning")
    saved_text = (tmp_path / "public/latest.json").read_text()
    saved = json.loads(saved_text)
    assert set(saved) == set(PUBLIC_FIELDS)
    assert not any(
        word in saved_text.lower()
        for word in ("cookie", "authorization", "password", "token")
    )
