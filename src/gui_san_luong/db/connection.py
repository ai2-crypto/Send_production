"""Open the SQLite database with the required settings and an up-to-date schema."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parents[3] / "migrations"
BUSY_TIMEOUT_MS = 5000


def _apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)"
    )
    applied = {
        row[0] for row in connection.execute("SELECT name FROM schema_migrations")
    }
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        # executescript commits on its own, so migrations must be safe to run again.
        connection.executescript(path.read_text(encoding="utf-8"))
        with connection:
            connection.execute(
                "INSERT INTO schema_migrations (name) VALUES (?)", (path.name,)
            )


@contextmanager
def open_database(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS:d}")
        _apply_migrations(connection)
        yield connection
    finally:
        connection.close()
