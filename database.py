"""Low-level SQLite connection configuration using the standard library only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5_000


def is_file_database(database_path: str | Path) -> bool:
    path = str(database_path)
    return path != ":memory:" and not (path.startswith("file:") and "mode=memory" in path)


def connect(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    read_only: bool = False,
) -> sqlite3.Connection:
    """Open a consistently configured SQLite connection."""
    path = str(database_path)
    if read_only and not is_file_database(path):
        raise ValueError("read-only connections require a file database")
    if is_file_database(path) and not read_only:
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    connection_path = path
    use_uri = path.startswith("file:")
    if read_only:
        connection_path = f"{Path(path).expanduser().resolve().as_uri()}?mode=ro"
        use_uri = True

    connection = sqlite3.connect(
        connection_path,
        timeout=busy_timeout_ms / 1_000,
        uri=use_uri,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    if read_only:
        connection.execute("PRAGMA query_only = ON")
    elif is_file_database(path):
        connection.execute("PRAGMA journal_mode = WAL")
    return connection
