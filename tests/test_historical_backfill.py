import json
from datetime import date, datetime

import pytest
import requests

import historical_backfill
from github_publisher import YANGON, merge_public_history, validate_public_record
from historical_backfill import (
    HistoricalBackfillError,
    HistoricalBackfillImporter,
    SecondaryHistoryClient,
    cli,
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
    parsed = parse_historical_day(
        payload(source_record(twod="99")),
        RESULT_DATE,
        IMPORTED_AT,
    )
    records, rejected = parsed
    assert records == []
    assert rejected == 1
    assert parsed.rejection_reasons == {"local_2d_verification_mismatch": 1}


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


class ExceptionSession:
    def __init__(self, exception):
        self.headers = {}
        self.exception = exception

    def get(self, *args, **kwargs):
        raise self.exception


@pytest.mark.parametrize(
    ("exception", "category"),
    [
        (requests.ConnectionError("dns failed"), "dns_or_connect_error"),
        (requests.ConnectTimeout("connect timed out"), "dns_or_connect_timeout"),
        (requests.exceptions.SSLError("certificate failed"), "tls_error"),
    ],
)
def test_network_failure_categories_are_distinct(exception, category):
    client = SecondaryHistoryClient(
        ExceptionSession(exception),
        max_attempts=1,
        sleep=lambda _: None,
    )
    with pytest.raises(HistoricalBackfillError) as captured:
        client.fetch_date(RESULT_DATE)
    assert captured.value.category == category


class Response:
    def __init__(self, body, *, status_code=200, content_type="application/json"):
        self.body = body
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

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


class StatusSession(CapturingSession):
    def __init__(self, status, *, content_type="application/problem+json"):
        super().__init__(None)
        self.status = status
        self.content_type = content_type

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(None, status_code=self.status, content_type=self.content_type)


@pytest.mark.parametrize(
    ("status", "category", "expected_calls"),
    [
        (403, "http_403", 1),
        (404, "http_404", 1),
        (429, "http_429", 2),
        (500, "http_5xx", 2),
        (503, "http_5xx", 2),
    ],
)
def test_http_failure_categories(status, category, expected_calls):
    session = StatusSession(status)
    events = []

    def log(event, **fields):
        events.append((event, fields))

    client = SecondaryHistoryClient(
        session,
        max_attempts=2,
        sleep=lambda _: None,
        log=log,
    )
    with pytest.raises(HistoricalBackfillError) as captured:
        client.fetch_date(RESULT_DATE)
    assert captured.value.category == category
    assert len(session.calls) == expected_calls
    response_events = [fields for event, fields in events if event == "http_response_received"]
    assert response_events[-1]["http_status"] == status
    assert response_events[-1]["content_type"] == "application/problem+json"
    assert response_events[-1]["endpoint_host"] == "api.thaistock2d.com"


def test_invalid_json_failure_is_distinct():
    class AlwaysNonJson(CapturingSession):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return NonJsonResponse(None, content_type="text/html")

    client = SecondaryHistoryClient(
        AlwaysNonJson(None),
        max_attempts=1,
        sleep=lambda _: None,
    )
    with pytest.raises(HistoricalBackfillError) as captured:
        client.fetch_date(RESULT_DATE)
    assert captured.value.category == "invalid_json"


def test_unexpected_response_schema_is_distinct():
    with pytest.raises(HistoricalBackfillError) as captured:
        parse_historical_day({"data": []}, RESULT_DATE, IMPORTED_AT)
    assert captured.value.category == "unexpected_response_schema"


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


def test_no_valid_records_is_distinct_and_dates_are_rate_limited(tmp_path):
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
    with pytest.raises(HistoricalBackfillError) as captured:
        importer.run(days=3)
    assert captured.value.category == "no_valid_records"
    assert client.calls == [date(2025, 7, 11), date(2025, 7, 12), date(2025, 7, 13)]
    assert sleeps == [0.25, 0.25]
    assert importer.report["dates_completed"] == 3
    assert not (tmp_path / "artifact").exists()


def test_all_local_verification_mismatches_fail_without_output(tmp_path):
    latest = official_record()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    client = FakeHistoryClient({RESULT_DATE: payload(source_record(twod="99"))})
    importer = HistoricalBackfillImporter(
        client,
        output_dir=tmp_path / "artifact",
        static_dir=static,
        remote_loader=remote_loader(latest, [latest]),
        base_url="https://example.test/",
        now=lambda: datetime(2025, 7, 11, 18, 0, tzinfo=YANGON),
        sleep=lambda _: None,
    )
    with pytest.raises(HistoricalBackfillError) as captured:
        importer.run(days=1)
    assert captured.value.category == "local_2d_verification_mismatch"
    assert importer.report["rejection_reasons"] == {
        "local_2d_verification_mismatch": 1
    }
    assert not (tmp_path / "artifact").exists()


def test_output_failure_preserves_previous_artifact(tmp_path):
    latest = official_record()
    output = tmp_path / "artifact"
    output.mkdir()
    (output / "latest.json").write_text("previous latest")
    (output / "history.json").write_text("previous history")
    client = FakeHistoryClient({RESULT_DATE: payload(source_record(open_time="11:00:00", twod="17"))})
    importer = HistoricalBackfillImporter(
        client,
        output_dir=output,
        static_dir=tmp_path / "missing-static",
        remote_loader=remote_loader(latest, [latest]),
        base_url="https://example.test/",
        now=lambda: datetime(2025, 7, 11, 18, 0, tzinfo=YANGON),
        sleep=lambda _: None,
    )
    with pytest.raises(HistoricalBackfillError) as captured:
        importer.run(days=1)
    assert captured.value.category == "output_file_failure"
    assert (output / "latest.json").read_text() == "previous latest"
    assert (output / "history.json").read_text() == "previous history"


def test_cli_prints_safe_failure_and_writes_failure_report(tmp_path, monkeypatch, capsys):
    class FailingImporter:
        def __init__(self, **kwargs):
            self.report = {
                "status": "running",
                "endpoint_host": "api.thaistock2d.com",
                "requested_start_date": "2025-07-01",
                "requested_end_date": "2025-07-30",
                "dates_requested": 30,
                "dates_completed": 2,
                "records_parsed": 4,
                "records_accepted": 0,
                "records_rejected": 4,
                "rejection_reasons": {"invalid_value": 4},
            }

        def run(self, **kwargs):
            raise HistoricalBackfillError("Safe network failure", category="http_403")

        def _record_failure(self, exc):
            self.report.update(
                status="failed",
                failure_category=exc.category,
                failure_message=str(exc),
            )

    monkeypatch.setattr(historical_backfill, "HistoricalBackfillImporter", FailingImporter)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    report_path = tmp_path / "report.json"
    assert cli(["--report-path", str(report_path)]) == 1
    captured = capsys.readouterr()
    assert '"failure_category": "http_403"' in captured.err
    assert "Safe network failure" in captured.err
    assert "::error title=Historical backfill failed::http_403: Safe network failure" in captured.err
    assert not any(secret in captured.err.lower() for secret in ("cookie", "authorization"))
    report = json.loads(report_path.read_text())
    assert report["failure_category"] == "http_403"

    assert cli(["--render-summary", str(report_path)]) == 0
    summary = capsys.readouterr().out
    assert "Status: failed" in summary
    assert "Failure category: http_403" in summary
    assert "Published JSON: unchanged" in summary
