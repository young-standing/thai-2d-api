from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import create_app, get_repository
from market_repository import MarketRepository


def sample(
    market_datetime="2026-07-11T03:20:14.587738578+07:00",
    *,
    last="1621.550000",
    value="77145337740",
):
    return {
        "last": last,
        "value": value,
        "marketDateTime": market_datetime,
        "marketStatus": "Closed",
        "change": "13.250000",
        "percentChange": "0.820000",
        "sourceClient": "playwright",
    }


def initialized_repository(tmp_path, name="api.sqlite3"):
    repository = MarketRepository(tmp_path / name)
    repository.initialize()
    return repository


def client_for(repository, now=None, *, raise_server_exceptions=True):
    application = create_app(repository, now=now)
    return TestClient(application, raise_server_exceptions=raise_server_exceptions)


def test_health_with_data(tmp_path, monkeypatch):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample())
    fetched_at = datetime.fromisoformat(repository.get_latest()["fetched_at"])
    monkeypatch.setenv("STALE_AFTER_SECONDS", "86400")
    with client_for(repository, lambda: fetched_at + timedelta(seconds=60)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "latest_available": True,
        "latest_fetched_at": repository.get_latest()["fetched_at"],
        "stale": False,
    }


def test_health_without_data(tmp_path):
    repository = initialized_repository(tmp_path)
    with client_for(repository) as client:
        response = client.get("/health")
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "latest_available": False,
        "latest_fetched_at": None,
        "stale": True,
    }


@pytest.mark.parametrize(("age", "expected"), [(60, False), (86401, True)])
def test_stale_and_non_stale_data(tmp_path, monkeypatch, age, expected):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample())
    fetched_at = datetime.fromisoformat(repository.get_latest()["fetched_at"])
    monkeypatch.setenv("STALE_AFTER_SECONDS", "86400")
    with client_for(repository, lambda: fetched_at + timedelta(seconds=age)) as client:
        assert client.get("/health").json()["stale"] is expected


@pytest.mark.parametrize("invalid", ["0", "-1", "1.5", "abc", ""])
def test_stale_after_seconds_must_be_positive_integer(tmp_path, monkeypatch, invalid):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample())
    monkeypatch.setenv("STALE_AFTER_SECONDS", invalid)
    with client_for(repository, raise_server_exceptions=False) as client:
        response = client.get("/api/2d/latest")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}


def test_latest_success_preserves_stored_strings(tmp_path):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample())
    with client_for(repository) as client:
        result = client.get("/api/market/latest")
    assert result.status_code == 200
    assert result.json()["last"] == "1621.550000"
    assert result.json()["value"] == "77145337740"


def test_latest_not_found_has_clear_json_error(tmp_path):
    repository = initialized_repository(tmp_path)
    with client_for(repository) as client:
        result = client.get("/api/market/latest")
    assert result.status_code == 404
    assert result.json() == {"detail": "No stored market data is available"}


@pytest.mark.parametrize("limit", [0, -1, 501, "abc", "1.5", True])
def test_history_limit_validation(tmp_path, limit):
    repository = initialized_repository(tmp_path)
    with client_for(repository) as client:
        result = client.get(f"/api/market/history?limit={str(limit).lower()}")
    assert result.status_code == 422


def test_history_ordering_newest_first(tmp_path):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample("2026-07-10T10:00:00+07:00", last="1.00"))
    repository.save_sample(sample("2026-07-12T10:00:00+07:00", last="3.00"))
    repository.save_sample(sample("2026-07-11T10:00:00+07:00", last="2.00"))
    with client_for(repository) as client:
        history = client.get("/api/market/history?limit=2").json()
    assert [row["last"] for row in history] == ["3.00", "2.00"]


def test_two_d_success(tmp_path, monkeypatch):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample())
    fetched = datetime.fromisoformat(repository.get_latest()["fetched_at"])
    monkeypatch.setenv("STALE_AFTER_SECONDS", "86400")
    with client_for(repository, lambda: fetched + timedelta(seconds=1)) as client:
        result = client.get("/api/2d/latest")
    assert result.status_code == 200
    assert result.json() == {
        "number": "55",
        "index_digit": "5",
        "value_digit": "5",
        "set_index": "1621.550000",
        "value_raw": "77145337740",
        "value_million": "77145.337740",
        "market_datetime": "2026-07-11T03:20:14.587738578+07:00",
        "market_status": "Closed",
        "fetched_at": repository.get_latest()["fetched_at"],
        "source_client": "playwright",
        "strategy": "set_hundredths_plus_value_million_units",
        "stale": False,
    }


def test_leading_zero_two_d_result(tmp_path):
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample(last="10.00", value="5000000"))
    with client_for(repository) as client:
        result = client.get("/api/2d/latest").json()
    assert result["number"] == "05"
    assert result["index_digit"] == "0"
    assert result["value_digit"] == "5"


def test_two_d_not_found(tmp_path):
    repository = initialized_repository(tmp_path)
    with client_for(repository) as client:
        response = client.get("/api/2d/latest")
    assert response.status_code == 404
    assert response.json() == {"detail": "No stored market data is available"}


class FailingRepository:
    def get_latest(self):
        raise RuntimeError("C:/secret/database/path.sqlite3 is unavailable")

    def get_history(self, limit):
        raise RuntimeError("C:/secret/database/path.sqlite3 is unavailable")


def test_repository_failure_returns_safe_internal_error(tmp_path):
    application = create_app(initialized_repository(tmp_path))
    application.dependency_overrides[get_repository] = lambda: FailingRepository()
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/api/market/latest")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret" not in response.text


def test_no_scraping_or_client_calls_during_api_requests(tmp_path, monkeypatch):
    from playwright_set_client import PlaywrightSetClient
    from unified_set_client import UnifiedSetClient

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("collection client must not be called by API")

    monkeypatch.setattr(PlaywrightSetClient, "fetch", forbidden)
    monkeypatch.setattr(UnifiedSetClient, "fetch", forbidden)
    repository = initialized_repository(tmp_path)
    repository.save_sample(sample())
    with client_for(repository) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/market/latest").status_code == 200
        assert client.get("/api/market/history").status_code == 200
        assert client.get("/api/2d/latest").status_code == 200


def test_cors_default_allows_no_origin(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    with client_for(initialized_repository(tmp_path)) as client:
        response = client.get("/health", headers={"Origin": "https://untrusted.example"})
    assert "access-control-allow-origin" not in response.headers


def test_cors_configured_origins(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_ORIGINS", "https://one.example, https://two.example"
    )
    with client_for(initialized_repository(tmp_path)) as client:
        allowed = client.get("/health", headers={"Origin": "https://two.example"})
        denied = client.get("/health", headers={"Origin": "https://other.example"})
    assert allowed.headers["access-control-allow-origin"] == "https://two.example"
    assert "access-control-allow-origin" not in denied.headers
