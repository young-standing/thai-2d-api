from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from three_d_publisher import (
    PublishedHistoryClient,
    ThreeDPublisher,
    ThreeDPublisherError,
    build_record,
    merge_history,
    most_recent_expected_draw,
    write_outputs_atomic,
)


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)


def sample(draw="2026-07-16", first="100007", fetched="2026-07-16T09:30:00+00:00"):
    return {
        "draw_date": draw,
        "first_prize": first,
        "source_updated_at": None,
        "fetched_at": fetched,
        "source": "https://www.glo.or.th/api/lottery/getLatestLottery",
        "source_client": "http",
    }


class Client:
    def __init__(self, values):
        self.values = list(values)

    async def fetch(self):
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def test_first_publication_and_leading_zero_round_trip(tmp_path):
    record = build_record(sample(), NOW)
    write_outputs_atomic(tmp_path, [record])
    latest = json.loads((tmp_path / "latest-3d.json").read_text())
    assert latest["three_d"] == "007"
    assert json.loads((tmp_path / "history-3d-all.json").read_text()) == [record]


def test_duplicate_older_draw_is_ignored_and_newer_replaces():
    old = build_record(sample(first="751495", fetched="2026-07-16T09:00:00+00:00"), NOW)
    older = build_record(sample(first="000123", fetched="2026-07-16T08:00:00+00:00"), NOW)
    newer = build_record(sample(first="000123", fetched="2026-07-16T10:00:00+00:00"), NOW)
    assert merge_history([old], older)[0]["first_prize"] == "751495"
    assert merge_history([old], newer)[0]["first_prize"] == "000123"


def test_history_newest_first_recent_50_and_all_not_truncated(tmp_path):
    records = []
    for i in range(60):
        month = 1 + i // 28
        day = 1 + i % 28
        records.append(build_record(sample(draw=f"2026-{month:02d}-{day:02d}"), NOW))
    records.sort(key=lambda item: item["draw_date"], reverse=True)
    write_outputs_atomic(tmp_path, records)
    assert len(json.loads((tmp_path / "history-3d.json").read_text())) == 50
    assert len(json.loads((tmp_path / "history-3d-all.json").read_text())) == 60
    assert json.loads((tmp_path / "latest-3d.json").read_text())["draw_date"] == records[0]["draw_date"]


@pytest.mark.asyncio
async def test_manual_smoke_changes_no_production_files(tmp_path):
    marker = tmp_path / "latest-3d.json"
    marker.write_text('{"legacy": true}', encoding="utf-8")
    publisher = ThreeDPublisher(client=Client([sample()]), output_dir=tmp_path, now=lambda: NOW)
    result = await publisher.smoke()
    assert result["three_d"] == "007"
    assert marker.read_text(encoding="utf-8") == '{"legacy": true}'


@pytest.mark.asyncio
async def test_failed_production_fetch_preserves_files(tmp_path):
    marker = tmp_path / "latest-3d.json"
    marker.write_text("{}", encoding="utf-8")
    publisher = ThreeDPublisher(client=Client([RuntimeError("down")]), output_dir=tmp_path, now=lambda: NOW)
    with pytest.raises(ThreeDPublisherError):
        await publisher.publish(expected_draw_date="2026-07-16")
    assert marker.read_text() == "{}"


@pytest.mark.asyncio
async def test_publish_rejects_wrong_expected_date_without_writes(tmp_path):
    publisher = ThreeDPublisher(client=Client([sample(draw="2026-07-01")]), output_dir=tmp_path, now=lambda: NOW)
    with pytest.raises(ThreeDPublisherError):
        await publisher.publish(expected_draw_date="2026-07-16")
    assert not (tmp_path / "latest-3d.json").exists()


@pytest.mark.asyncio
async def test_publish_success(tmp_path):
    publisher = ThreeDPublisher(
        client=Client([sample()]), history_loader=lambda: [], output_dir=tmp_path, now=lambda: NOW
    )
    result = await publisher.publish(expected_draw_date="2026-07-16")
    assert result["draw_date"] == "2026-07-16"


class RedirectResponse:
    status_code = 302


class RedirectSession:
    def get(self, *args, **kwargs):
        assert kwargs["allow_redirects"] is False
        return RedirectResponse()


def test_untrusted_history_redirect_rejected():
    with pytest.raises(ThreeDPublisherError, match="redirect"):
        PublishedHistoryClient(session=RedirectSession()).load()


def test_atomic_rollback_restores_previous_files(tmp_path, monkeypatch):
    old = build_record(sample(first="751495", fetched="2026-07-16T08:00:00+00:00"), NOW)
    write_outputs_atomic(tmp_path, [old])
    before = {path.name: path.read_bytes() for path in tmp_path.glob("*.json")}
    new = build_record(sample(first="000123", fetched="2026-07-16T10:00:00+00:00"), NOW)
    original = Path.replace
    calls = 0

    def failing_replace(self, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(ThreeDPublisherError, match="restored"):
        write_outputs_atomic(tmp_path, [new])
    assert {path.name: path.read_bytes() for path in tmp_path.glob("*.json")} == before


def test_stale_is_schedule_based_not_elapsed_days():
    between_draws = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    record = build_record(sample(draw="2026-07-01"), between_draws)
    assert record["stale"] is False


def test_expected_draw_changes_only_after_publication_cutoff():
    assert most_recent_expected_draw(datetime(2026, 7, 16, 9, 59, tzinfo=timezone.utc)).isoformat() == "2026-07-01"
    assert most_recent_expected_draw(datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)).isoformat() == "2026-07-16"
    assert most_recent_expected_draw(datetime(2026, 7, 1, 9, 59, tzinfo=timezone.utc)).isoformat() == "2026-06-16"


def test_pages_workflows_share_serialized_deployment_and_preserve_2d_files():
    root = Path(__file__).parents[1]
    three_d = (root / ".github/workflows/publish-3d.yml").read_text(encoding="utf-8")
    assert "group: github-pages-deployment" in three_d
    assert "cp -R public/." in three_d
    assert "--preserve-current-pages" in three_d
    assert "path: ${{ runner.temp }}/three-d-pages" in three_d
    for name in ("publish-2d.yml", "backfill-2d-history.yml"):
        text = (root / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "group: github-pages-deployment" in text
