"""Record each output message part sent to an employee, before and after calling Zalo."""

import sqlite3
from typing import NamedTuple


class SendRecord(NamedTuple):
    employee_code: str
    zalo_user_id: str
    content_hash: str
    part: int
    parts: int
    status: str
    zalo_message_id: str | None
    updated_at: str
    style: str = "text"


def fetch_day_sends(connection: sqlite3.Connection, day: str) -> list[SendRecord]:
    rows = connection.execute(
        "SELECT employee_code, zalo_user_id, content_hash, part, parts, status, "
        "zalo_message_id, updated_at, style FROM output_sends WHERE day = ? ORDER BY id",
        (day,),
    )
    return [SendRecord(*row) for row in rows]


def start_send(
    connection: sqlite3.Connection,
    day: str,
    record: SendRecord,
    actor: str,
    now: str,
) -> int:
    """Commit the attempt before the call, so a crash cannot hide a possible delivery."""
    with connection:
        cursor = connection.execute(
            "INSERT INTO output_sends (day, employee_code, zalo_user_id, content_hash, part, "
            "parts, status, zalo_message_id, created_by, created_at, updated_at, style) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (day, *record[:7], actor, now, now, record.style),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def finish_send(
    connection: sqlite3.Connection,
    send_id: int,
    status: str,
    zalo_message_id: str | None,
    error_code: int | None,
    now: str,
) -> None:
    with connection:
        connection.execute(
            "UPDATE output_sends SET status = ?, zalo_message_id = ?, error_code = ?, "
            "updated_at = ? WHERE id = ?",
            (status, zalo_message_id, error_code, now, send_id),
        )


def mark_interrupted(connection: sqlite3.Connection, now: str) -> int:
    """Parts left 'sending' by a run that stopped midway may have been delivered."""
    with connection:
        cursor = connection.execute(
            "UPDATE output_sends SET status = 'unknown', updated_at = ? "
            "WHERE status = 'sending'",
            (now,),
        )
    return cursor.rowcount


def count_style_since(connection: sqlite3.Connection, style: str, since: str) -> int:
    """Parts of one style attempted since a UTC time, e.g. images uploaded this month."""
    row = connection.execute(
        "SELECT COUNT(*) FROM output_sends WHERE style = ? AND created_at >= ?",
        (style, since),
    ).fetchone()
    return int(row[0])
