"""Store which Zalo account belongs to which employee code."""

import sqlite3
from collections.abc import Sequence
from typing import NamedTuple

ACTION_LINK_SET = "zalo_link_set"
ACTION_LINK_REMOVED = "zalo_link_removed"


class LinkRecord(NamedTuple):
    employee_code: str
    employee_name: str
    zalo_user_id: str


def fetch_links(connection: sqlite3.Connection) -> list[LinkRecord]:
    rows = connection.execute(
        "SELECT employee_code, employee_name, zalo_user_id "
        "FROM employee_zalo_links ORDER BY employee_code"
    )
    return [LinkRecord(*row) for row in rows]


def replace_links(
    connection: sqlite3.Connection,
    upserts: Sequence[LinkRecord],
    removals: Sequence[str],
    actor: str,
    now: str,
) -> None:
    """Apply all changes or none. Old rows go first so two people can swap accounts."""
    with connection:
        connection.executemany(
            "DELETE FROM employee_zalo_links WHERE employee_code = ?",
            [
                (code,)
                for code in (*removals, *(link.employee_code for link in upserts))
            ],
        )
        connection.executemany(
            "INSERT INTO employee_zalo_links "
            "(employee_code, employee_name, zalo_user_id, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(*link, actor, now) for link in upserts],
        )
        connection.executemany(
            "INSERT INTO audit_log (actor, action, target, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                *(
                    (actor, ACTION_LINK_SET, link.employee_code, now)
                    for link in upserts
                ),
                *((actor, ACTION_LINK_REMOVED, code, now) for code in removals),
            ],
        )
