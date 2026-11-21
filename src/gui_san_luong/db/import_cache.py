"""Store results read from workbooks, keyed by file content, in a throwaway SQLite file."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS import_cache ("
    "key TEXT PRIMARY KEY, value BLOB NOT NULL, used_at TEXT NOT NULL)"
)


@contextmanager
def open_cache(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS:d}")
        connection.execute(SCHEMA)
        yield connection
    finally:
        connection.close()


def fetch_entry(connection: sqlite3.Connection, key: str, now: str) -> bytes | None:
    row = connection.execute(
        "SELECT value FROM import_cache WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    with connection:
        connection.execute(
            "UPDATE import_cache SET used_at = ? WHERE key = ?", (now, key)
        )
    value: bytes = row[0]
    return value


def store_entry(
    connection: sqlite3.Connection,
    key: str,
    value: bytes,
    now: str,
    unused_since: str,
    max_entries: int,
) -> None:
    """Save one entry and drop old ones, so the file does not grow without limit."""
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO import_cache (key, value, used_at) VALUES (?, ?, ?)",
            (key, value, now),
        )
        connection.execute(
            "DELETE FROM import_cache WHERE used_at < ?", (unused_since,)
        )
        connection.execute(
            "DELETE FROM import_cache WHERE key NOT IN "
            "(SELECT key FROM import_cache ORDER BY used_at DESC LIMIT ?)",
            (max_entries,),
        )
