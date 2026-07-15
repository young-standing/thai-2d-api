import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from github_publisher import (
    PUBLIC_FIELDS,
    GitHubPublisher,
    GitHubPublisherError,
    YANGON,
    _arguments,
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
    source_yangon = datetime.fromisoformat(market_datetime).astimezone(YANGON)
    captured = source_yangon + timedelta(seconds=5)
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
        "captured_at_yangon": captured.isoformat(),
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


def publisher(tmp_path, client, clock, *, latest=None, history=None, logs=None):
    return GitHubPublisher(
        client,
        output_dir=tmp_path / "public",
        remote_loader=loader(latest, history),
        now=clock.now,
        sleep=clock.sleep,
        base_url="https://young-standing.github.io/thai-2d-api/",
        log=logs.append if logs is not None else lambda _: None,
    )


@pytest.mark.asyncio
async def test_morning_capture_exactly_at_target(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample(MORNING_SOURCE)]), clock, history=[]
    ).publish("morning", expected_session="morning")
    assert result["market_datetime"] == MORNING_SOURCE
    assert datetime.fromisoformat(MORNING_SOURCE).astimezone(YANGON).time().isoformat() == "12:01:00"


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
async def test_source_after_strict_window_is_rejected(tmp_path):
    prior = public_record(MORNING_SOURCE)
    after_window = sample("2026-07-13T17:05:01+07:00")
    clock = FakeClock(datetime(2026, 7, 13, 16, 36, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([after_window]), clock, latest=prior)
    with pytest.raises(GitHubPublisherError, match="expired"):
        await instance.publish("evening")
    assert not (tmp_path / "public/latest.json").exists()


@pytest.mark.asyncio
async def test_source_exactly_at_evening_window_end_is_accepted(tmp_path):
    prior = public_record(MORNING_SOURCE)
    at_window_end = "2026-07-13T17:05:00+07:00"
    clock = FakeClock(datetime(2026, 7, 13, 21, 45, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample(at_window_end)]), clock, latest=prior
    ).publish("evening")
    assert result["market_datetime"] == at_window_end


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
async def test_existing_result_date_and_session_is_rejected(tmp_path):
    prior = public_record("2026-07-13T12:31:00+07:00", session="morning")
    changed_same_session = sample("2026-07-13T12:31:30+07:00")
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, 30, tzinfo=YANGON))
    instance = publisher(
        tmp_path,
        FakeClient([changed_same_session]),
        clock,
        latest=prior,
        history=[prior],
    )
    with pytest.raises(GitHubPublisherError, match="expired"):
        await instance.publish("morning")
    assert not (tmp_path / "public/latest.json").exists()


@pytest.mark.asyncio
async def test_old_off_window_latest_does_not_block_valid_capture(tmp_path):
    invalid_old = public_record("2026-07-13T15:00:00+07:00", session="morning")
    assert invalid_old["market_datetime"] != MORNING_SOURCE
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path,
        FakeClient([sample(MORNING_SOURCE)]),
        clock,
        latest=invalid_old,
        history=[invalid_old],
    ).publish("morning")
    assert result["market_datetime"] == MORNING_SOURCE


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
async def test_manual_once_outside_collection_window_succeeds(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 3, 15, tzinfo=YANGON))
    client = FakeClient([sample("2026-07-12T22:00:00+07:00")])
    result = await publisher(tmp_path, client, clock).smoke("morning")
    assert result["publication_type"] == "smoke_test"
    assert client.calls == 1
    assert not (tmp_path / "public/latest.json").exists()


@pytest.mark.asyncio
async def test_delayed_scheduled_start_accepts_valid_source_timestamp(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 17, 45, tzinfo=YANGON))
    client = FakeClient([sample()])
    result = await publisher(tmp_path, client, clock, history=[]).publish("morning")
    assert result["market_datetime"] == MORNING_SOURCE
    assert client.calls == 1
    assert (tmp_path / "public/latest.json").exists()


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
async def test_success_marker_written_only_after_valid_files(tmp_path):
    marker = tmp_path / "production-published.json"
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([sample()]), clock, history=[])
    record = await instance.publish("morning")
    instance.write_success_marker(marker, record)
    assert json.loads(marker.read_text()) == {
        "market_datetime": MORNING_SOURCE,
        "production_published": True,
        "session": "morning",
    }


@pytest.mark.asyncio
async def test_failed_collection_never_creates_success_marker(tmp_path):
    marker = tmp_path / "production-published.json"
    clock = FakeClock(datetime(2026, 7, 13, 12, 3, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([RuntimeError("down")]), clock)
    with pytest.raises(GitHubPublisherError, match="not changed"):
        await instance.publish("morning")
    assert not marker.exists()


def test_stale_success_marker_is_removed_before_collection(tmp_path):
    marker = tmp_path / "production-published.json"
    marker.write_text('{"production_published": true}', encoding="utf-8")
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([sample()]), clock)
    assert instance.clear_success_marker(marker) == marker.resolve()
    assert not marker.exists()


@pytest.mark.asyncio
async def test_attempt_log_contains_bangkok_yangon_window_and_reason(tmp_path):
    logs = []
    before = sample("2026-07-13T12:30:59+07:00")
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([before, sample()]), clock, logs=logs)
    await instance.publish("morning")
    attempts = [item for item in logs if item["event"] == "fetch_attempt"]
    assert attempts[0]["source_bangkok_timestamp"] == "2026-07-13T12:30:59+07:00"
    assert attempts[0]["source_yangon_timestamp"] == "2026-07-13T12:00:59+06:30"
    assert attempts[0]["session_target_yangon"].endswith("T12:01:00+06:30")
    assert attempts[0]["session_window_end_yangon"].endswith("T12:06:00+06:30")
    assert attempts[0]["current_utc"].endswith("+00:00")
    assert "previous_published_market_datetime" in attempts[0]
    assert attempts[0]["source_market_datetime"] == "2026-07-13T12:30:59+07:00"
    assert attempts[0]["rejection_reason"] == "source_before_session_target"
    assert attempts[0]["decision_reason"] == "source_before_session_target"
    assert attempts[1]["accepted"] is True
    assert attempts[1]["decision_reason"] == "accepted"


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
async def test_history_is_newest_first_and_limited_to_200(tmp_path):
    base = datetime(2025, 12, 1, 12, 1, tzinfo=YANGON)
    history = [
        public_record((base + timedelta(days=index)).isoformat())
        for index in range(200)
    ]
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample()]), clock, history=history
    ).publish("morning")
    saved = json.loads((tmp_path / "public/history.json").read_text())
    assert len(saved) == 200
    assert saved[0]["market_datetime"] == result["market_datetime"]


def test_safe_static_output_paths(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([sample()]), clock)
    with pytest.raises(GitHubPublisherError, match="Unsafe"):
        instance._path("../secret.json")


def test_workflow_deploys_only_after_explicit_production_marker():
    workflow = (
        Path(__file__).parents[1]
        / ".github/workflows/publish-2d.yml"
    ).read_text(encoding="utf-8")
    assert "id: collection" in workflow
    assert "--publish-production" in workflow
    assert "PRODUCTION_SUCCESS_MARKER: ${{ runner.temp }}/production-published.json" in workflow
    assert "production_published=true" in workflow
    assert workflow.count("steps.collection.outputs.production_published == 'true'") == 3
    assert workflow.count("steps.selection.outputs.mode == 'publish'") == 3
    assert workflow.count("success() &&") >= 3
    assert "github.event_name=$EVENT_NAME" in workflow
    assert "github.event.schedule=$EVENT_SCHEDULE" in workflow
    assert 'mode="publish"' in workflow
    assert 'default: once' in workflow
    assert 'if [[ "$SELECTED_MODE" == "once" ]]' in workflow
    assert 'elif [[ "$SELECTED_MODE" == "publish" ]]' in workflow
    assert 'echo "Unknown mode: $SELECTED_MODE"' in workflow


def test_cli_requires_one_explicit_mode():
    with pytest.raises(SystemExit) as missing:
        _arguments(["--window", "morning"])
    assert missing.value.code == 2


def test_cli_rejects_both_modes():
    with pytest.raises(SystemExit) as conflicting:
        _arguments(
            ["--window", "morning", "--once", "--publish-production"]
        )
    assert conflicting.value.code == 2


def test_cli_accepts_once_and_publish_production_separately():
    once = _arguments(["--window", "morning", "--once"])
    production = _arguments(
        ["--window", "evening", "--publish-production"]
    )
    assert once.once is True and once.publish_production is False
    assert production.once is False and production.publish_production is True


def test_publisher_prints_explicit_startup_mode_fields():
    source = (Path(__file__).parents[1] / "github_publisher.py").read_text(
        encoding="utf-8"
    )
    for field in (
        '"event": "publisher_mode"',
        '"selected_session": session',
        '"mode": mode',
        '"github_event_name":',
        '"github_schedule":',
    ):
        assert field in source


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
