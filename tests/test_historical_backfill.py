import json
from datetime import date, datetime

import pytest
import requests

from github_publisher import YANGON, merge_public_history, validate_public_record
from historical_backfill import (
    HistoricalBackfillError,
    HistoricalBackfillImporter,
    SecondaryHistoryClient,
    parse_historical_day,
)


RESULT_DATE = date(2025, 7, 11)
IMPORTED_AT = datetime(2025, 8, 1, 10, 0, tzinfo=YANGON)


def source_record(
    *,
    open_time="12:01:00",
    set_value="1,127.71",
    value="18,447.32",
    twod="17",
    **extra,
):
    return {
        "time": open_time,
        "set": set_value,
        "value": value,
        "twod": twod,
        **extra,
    }


def payload(*records, result_date="2025-07-11"):
    return [{"date": result_date, "child": list(records)}]


def official_record(*, result_date="2025-07-11", session="morning"):
    target = "12:01:00" if session == "morning" else "16:30:00"
    thailand_time = "12:31:00" if session == "morning" else "17:00:00"
    market_datetime = f"{result_date}T{thailand_time}+07:00"
    return {
        "number": "17",
        "index_digit": "1",
        "value_digit": "7",
        "set_index": "1127.71",
        "value_raw": "18447320000",
        "value_million": "18447.320000",
        "market_datetime": market_datetime,
        "market_status": "Closed",
        "fetched_at": f"{result_date}T05:31:05+00:00",
        "source_client": "playwright",
        "strategy": "set_hundredths_plus_value_million_units",
        "session": session,
        "target_time_yangon": target,
        "captured_at_yangon": f"{result_date}T{target}+06:30",
        "source_market_datetime": market_datetime,
        "publication_type": "scheduled_result",
        "stale": False,
    }


def test_malformed_payload_and_records_are_rejected():
    with pytest.raises(HistoricalBackfillError, match="must be a list"):
        parse_historical_day({"date": "2025-07-11"}, RESULT_DATE, IMPORTED_AT)
    records, rejected = parse_historical_day(
        payload({"time": "12:01:00", "set": "bad", "value": "1.00", "twod": "01"}),
        RESULT_DATE,
        IMPORTED_AT,
    )
    assert records == []
    assert rejected == 1


def test_mismatched_supplied_result_is_rejected():
    records, rejected = parse_historical_day(
        payload(source_record(twod="99")),
        RESULT_DATE,
        IMPORTED_AT,
    )
    assert records == []
    assert rejected == 1


def test_leading_zero_result_is_preserved_and_verified():
    records, rejected = parse_historical_day(
        payload(source_record(set_value="1,100.00", value="12,345.67", twod="05")),
        RESULT_DATE,
        IMPORTED_AT,
    )
    assert rejected == 0
    assert records[0]["number"] == "05"
    assert records[0]["index_digit"] == "0"
    assert records[0]["value_digit"] == "5"
    assert records[0]["set_index"] == "1100.00"
    assert records[0]["value_million"] == "12345.67"
    assert records[0]["verified_locally"] is True
    assert validate_public_record(records[0]) == records[0]


def test_duplicate_date_and_session_is_imported_once():
    record = source_record(history_id="ignored")
    records, rejected = parse_historical_day(
        payload(record, dict(record)),
        RESULT_DATE,
        IMPORTED_AT,
    )
    assert len(records) == 1
    assert rejected == 1
    assert "history_id" not in records[0]


class FailingSession:
    def __init__(self):
        self.headers = {}
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        raise requests.Timeout("timed out")


def test_network_failures_retry_with_exponential_backoff():
    session = FailingSession()
    sleeps = []
    client = SecondaryHistoryClient(session, sleep=sleeps.append)
    with pytest.raises(HistoricalBackfillError, match="after 4 attempts"):
        client.fetch_date(RESULT_DATE)
    assert session.calls == 4
    assert sleeps == [1, 2, 4]


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class NonJsonResponse(Response):
    def json(self):
        raise requests.JSONDecodeError("invalid", "not-json", 0)


class CapturingSession:
    def __init__(self, body):
        self.headers = {}
        self.body = body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.body)


def test_client_uses_separate_dd_mm_yyyy_request_and_timeout():
    session = CapturingSession([])
    client = SecondaryHistoryClient(session)
    assert client.fetch_date(RESULT_DATE) == []
    assert session.calls == [
        (
            "https://api.thaistock2d.com/2d_result",
            {"params": {"date": "11-07-2025"}, "timeout": 15},
        )
    ]


def test_client_uses_narrow_iso_fallback_when_documented_format_is_not_json():
    class FormatSession(CapturingSession):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return NonJsonResponse(None)
            return Response([])

    session = FormatSession([])
    sleeps = []
    client = SecondaryHistoryClient(session, sleep=sleeps.append)
    assert client.fetch_date(RESULT_DATE) == []
    assert [call[1]["params"]["date"] for call in session.calls] == [
        "11-07-2025",
        "2025-07-11",
    ]
    assert sleeps == [0.5]


def test_official_record_has_priority_over_backfill():
    backfilled, rejected = parse_historical_day(
        payload(source_record()),
        RESULT_DATE,
        IMPORTED_AT,
    )
    official = official_record()
    merged = merge_public_history([backfilled[0], official])
    assert rejected == 0
    assert len(merged) == 1
    assert merged[0]["publication_type"] == "scheduled_result"


class FakeHistoryClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def fetch_date(self, result_date):
        self.calls.append(result_date)
        outcome = self.outcomes.get(result_date, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def remote_loader(latest, history):
    def load(url):
        if url.endswith("latest.json"):
            return latest
        if url.endswith("history.json"):
            return history
        raise AssertionError(url)

    return load


def test_importer_preserves_official_latest_and_priority(tmp_path):
    latest = official_record()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    client = FakeHistoryClient({RESULT_DATE: payload(source_record())})
    importer = HistoricalBackfillImporter(
        client,
        output_dir=tmp_path / "artifact",
        static_dir=static,
        remote_loader=remote_loader(latest, [latest]),
        base_url="https://example.test/",
        now=lambda: datetime(2025, 7, 11, 18, 0, tzinfo=YANGON),
        sleep=lambda _: None,
    )
    report = importer.run(days=1)
    saved_latest = json.loads((tmp_path / "artifact/latest.json").read_text())
    saved_history = json.loads((tmp_path / "artifact/history.json").read_text())
    assert saved_latest == latest
    assert len(saved_history) == 1
    assert saved_history[0]["publication_type"] == "scheduled_result"
    assert report["records_imported"] == 1


def test_network_failure_writes_no_artifact(tmp_path):
    latest = official_record()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    failure = HistoricalBackfillError("network failed")
    client = FakeHistoryClient({RESULT_DATE: failure})
    importer = HistoricalBackfillImporter(
        client,
        output_dir=tmp_path / "artifact",
        static_dir=static,
        remote_loader=remote_loader(latest, [latest]),
        base_url="https://example.test/",
        now=lambda: datetime(2025, 7, 11, 18, 0, tzinfo=YANGON),
        sleep=lambda _: None,
    )
    with pytest.raises(HistoricalBackfillError, match="network failed"):
        importer.run(days=1)
    assert not (tmp_path / "artifact").exists()


def test_importer_fetches_each_calendar_date_with_rate_limiting(tmp_path):
    latest = official_record()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    client = FakeHistoryClient({})
    sleeps = []
    importer = HistoricalBackfillImporter(
        client,
        output_dir=tmp_path / "artifact",
        static_dir=static,
        remote_loader=remote_loader(latest, [latest]),
        base_url="https://example.test/",
        now=lambda: datetime(2025, 7, 13, 18, 0, tzinfo=YANGON),
        sleep=sleeps.append,
        rate_limit_seconds=0.25,
    )
    report = importer.run(days=3)
    assert client.calls == [date(2025, 7, 11), date(2025, 7, 12), date(2025, 7, 13)]
    assert sleeps == [0.25, 0.25]
    assert report["dates_fetched"] == 3
