import json
from datetime import datetime, timedelta

import pytest

from github_publisher import (
    PUBLIC_FIELDS,
    GitHubPublisher,
    GitHubPublisherError,
    YANGON,
)


def sample(
    market_datetime="2026-07-13T12:31:01+07:00",
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


def public_record(market_datetime, *, session="morning", number="55"):
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
        "stale": False,
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
async def test_successful_morning_publication(tmp_path):
    prior = public_record("2026-07-12T17:00:00+07:00", session="evening")
    clock = FakeClock(datetime(2026, 7, 13, 11, 58, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([sample()]), clock, latest=prior, history=[prior]
    ).publish("morning")
    assert result["number"] == "55"
    assert result["session"] == "morning"
    assert json.loads((tmp_path / "public/latest.json").read_text())["number"] == "55"


@pytest.mark.asyncio
async def test_successful_evening_publication(tmp_path):
    prior = public_record("2026-07-13T12:31:00+07:00")
    evening = sample("2026-07-13T17:00:01+07:00")
    clock = FakeClock(datetime(2026, 7, 13, 16, 27, tzinfo=YANGON))
    result = await publisher(
        tmp_path, FakeClient([evening]), clock, latest=prior, history=[prior]
    ).publish("evening")
    assert result["session"] == "evening"
    assert result["market_datetime"] == evening["marketDateTime"]


@pytest.mark.asyncio
async def test_leading_zero_result(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    result = await publisher(
        tmp_path,
        FakeClient([sample(last="10.00", value="5000000")]),
        clock,
        history=[],
    ).publish("morning")
    assert result["number"] == "05"
    assert json.loads((tmp_path / "public/latest.json").read_text())["number"] == "05"


@pytest.mark.asyncio
async def test_duplicate_history_prevention(tmp_path):
    current = public_record(sample()["marketDateTime"])
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    await publisher(
        tmp_path, FakeClient([sample()]), clock, history=[current, current]
    ).publish("morning")
    history = json.loads((tmp_path / "public/history.json").read_text())
    assert len(history) == 1
    assert history[0]["market_datetime"] == current["market_datetime"]


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


@pytest.mark.asyncio
async def test_malformed_previous_history_starts_empty(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    await publisher(
        tmp_path,
        FakeClient([sample()]),
        clock,
        history={"token": "must-not-be-published"},
    ).publish("morning")
    saved = json.loads((tmp_path / "public/history.json").read_text())
    assert len(saved) == 1
    assert "token" not in saved[0]


@pytest.mark.asyncio
async def test_one_malformed_record_invalidates_previous_history(tmp_path):
    valid = public_record("2026-07-12T17:00:00+07:00", session="evening")
    malformed = {**valid, "source_client": "token=secret"}
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    await publisher(
        tmp_path, FakeClient([sample()]), clock, history=[valid, malformed]
    ).publish("morning")
    saved = json.loads((tmp_path / "public/history.json").read_text())
    assert len(saved) == 1
    assert saved[0]["market_datetime"] == sample()["marketDateTime"]


@pytest.mark.asyncio
async def test_failed_fetch_does_not_overwrite_valid_files(tmp_path):
    output = tmp_path / "public"
    output.mkdir()
    (output / "latest.json").write_text('{"valid":"latest"}\n')
    (output / "history.json").write_text('[{"valid":"history"}]\n')
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    instance = publisher(tmp_path, FakeClient([RuntimeError("failed")]), clock)
    with pytest.raises(GitHubPublisherError, match="not changed"):
        await instance.publish("morning", poll=False)
    assert (output / "latest.json").read_text() == '{"valid":"latest"}\n'
    assert (output / "history.json").read_text() == '[{"valid":"history"}]\n'


@pytest.mark.asyncio
async def test_json_numeric_values_are_strings_not_floats(tmp_path):
    clock = FakeClock(datetime(2026, 7, 13, 12, 1, tzinfo=YANGON))
    await publisher(tmp_path, FakeClient([sample()]), clock, history=[]).publish("morning")
    saved = json.loads((tmp_path / "public/latest.json").read_text())
    for field in ("number", "index_digit", "value_digit", "set_index", "value_raw", "value_million"):
        assert isinstance(saved[field], str)


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
    assert not any(word in saved_text.lower() for word in ("cookie", "authorization", "password", "token"))
