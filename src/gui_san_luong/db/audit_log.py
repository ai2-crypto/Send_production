"""Append-only record of who did what, for actions that reach outside the company."""

import sqlite3


def append_audit(
    connection: sqlite3.Connection, actor: str, action: str, target: str, now: str
) -> None:
    with connection:
        connection.execute(
            "INSERT INTO audit_log (actor, action, target, created_at) VALUES (?, ?, ?, ?)",
            (actor, action, target, now),
        )
