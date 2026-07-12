import copy
import sqlite3

import pytest

from database import connect
from market_repository import MarketRepository, MarketRepositoryError


def sample(
    market_datetime="2026-07-11T03:20:14.587738578+07:00",
    *,
    last="1621.550000",
):
    return {
        "last": last,
        "value": "77145337740",
        "marketDateTime": market_datetime,
        "marketStatus": "Closed",
        "change": "13.250000",
        "percentChange": "0.820000",
        "sourceClient": "playwright",
    }


@pytest.fixture
def repository(tmp_path):
    repo = MarketRepository(tmp_path / "market.sqlite3")
    repo.initialize()
    return repo


def test_initial_schema_creation(repository):
    with connect(repository.database_path) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
            )
        }
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    assert {"latest_market_data", "market_history"} <= tables
    assert foreign_keys == 1
    assert journal_mode.lower() == "wal"
    assert busy_timeout > 0


def test_first_insert(repository):
    result = repository.save_sample(sample())
    latest = repository.get_latest()
    assert result == {"latest_updated": True, "history_inserted": True}
    assert latest["id"] == 1
    assert latest["last"] == "1621.550000"
    assert latest["value"] == "77145337740"
    assert latest["market_datetime"] == sample()["marketDateTime"]
    assert latest["fetched_at"].endswith("+00:00")


def test_latest_row_update(repository):
    repository.save_sample(sample())
    newer = sample("2026-07-12T10:00:00+07:00", last="1630.000000")
    repository.save_sample(newer)
    latest = repository.get_latest()
    assert latest["id"] == 1
    assert latest["last"] == "1630.000000"
    assert len(repository.get_history()) == 2


def test_history_duplicate_prevention_still_updates_latest(repository):
    original = sample()
    repository.save_sample(original)
    duplicate_time = {**original, "last": "1622.000000", "sourceClient": "requests"}
    result = repository.save_sample(duplicate_time)
    assert result == {"latest_updated": True, "history_inserted": False}
    assert repository.get_latest()["last"] == "1622.000000"
    assert len(repository.get_history()) == 1


def test_input_is_not_mutated(repository):
    original = sample()
    before = copy.deepcopy(original)
    repository.save_sample(original)
    assert original == before


def test_missing_and_extra_fields_are_rejected(repository):
    missing = sample()
    del missing["value"]
    with pytest.raises(MarketRepositoryError, match="missing fields"):
        repository.save_sample(missing)
    with pytest.raises(MarketRepositoryError, match="unexpected fields"):
        repository.save_sample({**sample(), "extra": "value"})
    assert repository.get_latest() is None


@pytest.mark.parametrize("invalid", [1.2, 1, True, False])
def test_float_int_and_bool_numeric_values_are_rejected(repository, invalid):
    invalid_sample = sample()
    invalid_sample["last"] = invalid
    with pytest.raises(MarketRepositoryError, match="must be a string"):
        repository.save_sample(invalid_sample)


@pytest.mark.parametrize("invalid", ["7.714533774E+10", "1e3", "NaN", "Infinity", "-Infinity"])
def test_scientific_and_non_finite_values_are_rejected(repository, invalid):
    invalid_sample = sample()
    invalid_sample["value"] = invalid
    with pytest.raises(MarketRepositoryError, match="plain decimal notation"):
        repository.save_sample(invalid_sample)


@pytest.mark.parametrize("field", ["last", "value", "marketDateTime", "marketStatus", "change", "percentChange", "sourceClient"])
def test_empty_strings_are_rejected(repository, field):
    invalid_sample = sample()
    invalid_sample[field] = "   "
    with pytest.raises(MarketRepositoryError, match="must not be empty"):
        repository.save_sample(invalid_sample)


def test_full_transaction_rolls_back_if_history_insert_fails(repository):
    original = sample()
    repository.save_sample(original)
    failing_datetime = "2026-07-12T10:00:00+07:00"
    with connect(repository.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_history_insert
            BEFORE INSERT ON market_history
            WHEN NEW.market_datetime = '2026-07-12T10:00:00+07:00'
            BEGIN
                SELECT RAISE(ABORT, 'forced history failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced history failure"):
        repository.save_sample(sample(failing_datetime, last="1700.000000"))

    assert repository.get_latest()["last"] == original["last"]
    assert len(repository.get_history()) == 1


def test_reconnect_persistence(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    first = MarketRepository(path)
    first.initialize()
    first.save_sample(sample())
    second = MarketRepository(path)
    assert second.get_latest()["value"] == "77145337740"
    assert len(second.get_history()) == 1


def test_history_is_ordered_newest_first(repository):
    repository.save_sample(sample("2026-07-10T10:00:00+07:00", last="1.00"))
    repository.save_sample(sample("2026-07-12T10:00:00+07:00", last="3.00"))
    repository.save_sample(sample("2026-07-11T10:00:00+07:00", last="2.00"))
    assert [row["last"] for row in repository.get_history()] == ["3.00", "2.00", "1.00"]
    assert len(repository.get_history(limit=2)) == 2


@pytest.mark.parametrize("invalid", [True, False, 1.5, "10", None, 0, -1, 501])
def test_history_limit_validation(repository, invalid):
    with pytest.raises(MarketRepositoryError, match="history limit"):
        repository.get_history(invalid)


@pytest.mark.parametrize("valid", [1, 50, 500])
def test_history_limit_boundaries(repository, valid):
    assert repository.get_history(valid) == []
