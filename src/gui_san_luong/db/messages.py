"""Store messages read from Zalo, one row per message id."""

import sqlite3
from collections.abc import Sequence
from typing import NamedTuple


class MessageRecord(NamedTuple):
    msg_id: str
    zalo_user_id: str
    direction: int
    sent_at_ms: int
    user_display_name: str | None
    message_json: str
    fetched_at: str


def upsert_messages(
    connection: sqlite3.Connection, records: Sequence[MessageRecord]
) -> None:
    """A message read again keeps its row; only the name and read time are refreshed."""
    with connection:
        connection.executemany(
            "INSERT INTO messages (msg_id, zalo_user_id, direction, sent_at_ms, "
            "user_display_name, message_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(msg_id) DO UPDATE SET "
            "user_display_name = COALESCE(excluded.user_display_name, user_display_name), "
            "fetched_at = excluded.fetched_at",
            records,
        )


def fetch_messages(connection: sqlite3.Connection) -> list[MessageRecord]:
    rows = connection.execute(
        "SELECT msg_id, zalo_user_id, direction, sent_at_ms, user_display_name, "
        "message_json, fetched_at FROM messages ORDER BY zalo_user_id, sent_at_ms"
    )
    return [MessageRecord(*row) for row in rows]


def delete_messages(connection: sqlite3.Connection) -> int:
    with connection:
        return connection.execute("DELETE FROM messages").rowcount
