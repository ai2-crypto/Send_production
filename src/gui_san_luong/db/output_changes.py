"""Store the last version of each report and the output changes found between versions."""

import sqlite3
from collections.abc import Sequence
from typing import NamedTuple


class SnapshotRecord(NamedTuple):
    source_key: str
    file_name: str
    file_digest: str
    outputs_digest: str
    outputs_json: str


class ChangeRecord(NamedTuple):
    id: int
    detected_at: str
    file_name: str
    day: str
    employee_code: str
    employee_name: str
    line: str
    unit: str | None
    old_quantity: float | None
    new_quantity: float | None
    excel_row: int | None


class NewChange(NamedTuple):
    day: str
    employee_code: str
    employee_name: str
    line: str
    unit: str | None
    old_quantity: float | None
    new_quantity: float | None
    excel_row: int | None = None


def fetch_snapshot(
    connection: sqlite3.Connection, source_key: str
) -> SnapshotRecord | None:
    row = connection.execute(
        "SELECT source_key, file_name, file_digest, outputs_digest, outputs_json "
        "FROM output_snapshots WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    return SnapshotRecord(*row) if row else None


def save_version(
    connection: sqlite3.Connection,
    snapshot: SnapshotRecord,
    changes: Sequence[NewChange],
    now: str,
) -> None:
    """Replace the snapshot and log its changes together, so no change is logged twice."""
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO output_snapshots "
            "(source_key, file_name, file_digest, outputs_digest, outputs_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (*snapshot, now),
        )
        connection.executemany(
            "INSERT INTO output_changes (detected_at, source_key, file_name, day, "
            "employee_code, employee_name, line, unit, old_quantity, new_quantity, excel_row) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (now, snapshot.source_key, snapshot.file_name, *change)
                for change in changes
            ],
        )


def fetch_open_changes(connection: sqlite3.Connection) -> list[ChangeRecord]:
    rows = connection.execute(
        "SELECT id, detected_at, file_name, day, employee_code, employee_name, line, unit, "
        "old_quantity, new_quantity, excel_row FROM output_changes WHERE handled_at IS NULL "
        "ORDER BY detected_at DESC, day, employee_code, excel_row, line"
    )
    return [ChangeRecord(*row) for row in rows]


def mark_handled(
    connection: sqlite3.Connection, change_ids: Sequence[int], now: str
) -> None:
    with connection:
        connection.executemany(
            "UPDATE output_changes SET handled_at = ? WHERE id = ? AND handled_at IS NULL",
            [(now, change_id) for change_id in change_ids],
        )
