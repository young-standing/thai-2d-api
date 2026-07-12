"""SQLite persistence for latest and historical SET market samples."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from database import DEFAULT_BUSY_TIMEOUT_MS, connect

API_FIELDS = (
    "last",
    "value",
    "marketDateTime",
    "marketStatus",
    "change",
    "percentChange",
    "sourceClient",
)
NUMERIC_FIELDS = ("last", "value", "change", "percentChange")
PLAIN_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


class MarketRepositoryError(ValueError):
    """Raised when repository input or query arguments are invalid."""


class MarketRepository:
    def __init__(
        self,
        database_path: str | Path = "thai_2d.sqlite3",
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ):
        self.database_path = str(database_path)
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        return connect(
            self.database_path,
            busy_timeout_ms=self.busy_timeout_ms,
            read_only=read_only,
        )

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS latest_market_data (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last TEXT NOT NULL,
                    value TEXT NOT NULL,
                    market_datetime TEXT NOT NULL,
                    market_status TEXT NOT NULL,
                    change TEXT NOT NULL,
                    percent_change TEXT NOT NULL,
                    source_client TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    last TEXT NOT NULL,
                    value TEXT NOT NULL,
                    market_datetime TEXT NOT NULL UNIQUE,
                    market_status TEXT NOT NULL,
                    change TEXT NOT NULL,
                    percent_change TEXT NOT NULL,
                    source_client TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _validate_sample(sample: Any) -> dict[str, str]:
        if not isinstance(sample, dict):
            raise MarketRepositoryError("sample must be a dictionary")

        missing = [field for field in API_FIELDS if field not in sample]
        extra = [field for field in sample if field not in API_FIELDS]
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if extra:
                details.append(f"unexpected fields: {', '.join(extra)}")
            raise MarketRepositoryError(f"sample schema error ({'; '.join(details)})")

        validated: dict[str, str] = {}
        for field in API_FIELDS:
            value = sample[field]
            if not isinstance(value, str):
                raise MarketRepositoryError(f"field '{field}' must be a string")
            if not value.strip():
                raise MarketRepositoryError(f"field '{field}' must not be empty")
            validated[field] = value

        for field in NUMERIC_FIELDS:
            value = validated[field]
            if "e" in value.lower() or not PLAIN_DECIMAL_PATTERN.fullmatch(value):
                raise MarketRepositoryError(
                    f"numeric field '{field}' must use plain decimal notation"
                )
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise MarketRepositoryError(f"numeric field '{field}' is invalid") from exc
            if not number.is_finite():
                raise MarketRepositoryError(f"numeric field '{field}' must be finite")

        return validated

    def save_sample(self, sample: Any) -> dict[str, bool]:
        validated = self._validate_sample(sample)
        fetched_at = datetime.now(timezone.utc).isoformat()
        values = (
            validated["last"],
            validated["value"],
            validated["marketDateTime"],
            validated["marketStatus"],
            validated["change"],
            validated["percentChange"],
            validated["sourceClient"],
            fetched_at,
        )

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO latest_market_data (
                    id, last, value, market_datetime, market_status,
                    change, percent_change, source_client, fetched_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last = excluded.last,
                    value = excluded.value,
                    market_datetime = excluded.market_datetime,
                    market_status = excluded.market_status,
                    change = excluded.change,
                    percent_change = excluded.percent_change,
                    source_client = excluded.source_client,
                    fetched_at = excluded.fetched_at
                """,
                values,
            )
            history_cursor = connection.execute(
                """
                INSERT INTO market_history (
                    last, value, market_datetime, market_status,
                    change, percent_change, source_client, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_datetime) DO NOTHING
                """,
                values,
            )

        return {"latest_updated": True, "history_inserted": history_cursor.rowcount == 1}

    def get_latest(self) -> dict[str, Any] | None:
        with self._connect(read_only=True) as connection:
            row = connection.execute(
                "SELECT * FROM latest_market_data WHERE id = ?", (1,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise MarketRepositoryError("history limit must be an integer")
        if not 1 <= limit <= 500:
            raise MarketRepositoryError("history limit must be between 1 and 500")

        with self._connect(read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM market_history
                ORDER BY market_datetime DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
