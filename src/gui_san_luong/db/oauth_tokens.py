"""Store the OA token pair. Tokens arrive here already encrypted."""

import sqlite3
from typing import NamedTuple

TOKEN_ROW = "oa"
ACTION_TOKENS_SET = "zalo_tokens_set"
ACTION_TOKENS_REFRESHED = "zalo_tokens_refreshed"
ACTION_TOKENS_REMOVED = "zalo_tokens_removed"


class TokenRecord(NamedTuple):
    access_token_enc: str
    refresh_token_enc: str
    access_expires_at: str
    refresh_expires_at: str
    updated_at: str
    last_error: str | None
    needs_reauthorization: bool
    retry_after: str | None


def fetch_tokens(connection: sqlite3.Connection) -> TokenRecord | None:
    row = connection.execute(
        "SELECT access_token_enc, refresh_token_enc, access_expires_at, "
        "refresh_expires_at, updated_at, last_error, needs_reauthorization, retry_after "
        "FROM oauth_tokens WHERE name = ?",
        (TOKEN_ROW,),
    ).fetchone()
    if row is None:
        return None
    access, refresh, access_expires, refresh_expires, updated, error, reauth, retry = (
        row
    )
    return TokenRecord(
        access,
        refresh,
        access_expires,
        refresh_expires,
        updated,
        error,
        bool(reauth),
        retry,
    )


def save_tokens(
    connection: sqlite3.Connection,
    access_token_enc: str,
    refresh_token_enc: str,
    access_expires_at: str,
    refresh_expires_at: str,
    now: str,
    action: str,
) -> None:
    """Replace the pair, clear any error and lease, all in one transaction."""
    with connection:
        connection.execute(
            "INSERT INTO oauth_tokens (name, access_token_enc, refresh_token_enc, "
            "access_expires_at, refresh_expires_at, updated_at, refresh_lease_until, "
            "last_error, needs_reauthorization, retry_after) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL) "
            "ON CONFLICT(name) DO UPDATE SET access_token_enc = excluded.access_token_enc, "
            "refresh_token_enc = excluded.refresh_token_enc, "
            "access_expires_at = excluded.access_expires_at, "
            "refresh_expires_at = excluded.refresh_expires_at, "
            "updated_at = excluded.updated_at, refresh_lease_until = NULL, "
            "last_error = NULL, needs_reauthorization = 0, retry_after = NULL",
            (
                TOKEN_ROW,
                access_token_enc,
                refresh_token_enc,
                access_expires_at,
                refresh_expires_at,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO audit_log (actor, action, target, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("app", action, TOKEN_ROW, now),
        )


def claim_refresh(connection: sqlite3.Connection, now: str, lease_until: str) -> bool:
    """Take the right to refresh, unless someone else holds an unexpired lease.

    A single UPDATE, so two callers can never both win.
    """
    with connection:
        cursor = connection.execute(
            "UPDATE oauth_tokens SET refresh_lease_until = ? WHERE name = ? "
            "AND needs_reauthorization = 0 "
            "AND (refresh_lease_until IS NULL OR refresh_lease_until <= ?)",
            (lease_until, TOKEN_ROW, now),
        )
    return cursor.rowcount == 1


def record_failure(
    connection: sqlite3.Connection,
    error: str,
    needs_reauthorization: bool,
    retry_after: str | None,
) -> None:
    """Keep the pair, release the lease, and remember why the refresh failed."""
    with connection:
        connection.execute(
            "UPDATE oauth_tokens SET refresh_lease_until = NULL, last_error = ?, "
            "needs_reauthorization = ?, retry_after = ? WHERE name = ?",
            (error, int(needs_reauthorization), retry_after, TOKEN_ROW),
        )


def delete_tokens(connection: sqlite3.Connection, now: str) -> None:
    with connection:
        connection.execute("DELETE FROM oauth_tokens WHERE name = ?", (TOKEN_ROW,))
        connection.execute(
            "INSERT INTO audit_log (actor, action, target, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("app", ACTION_TOKENS_REMOVED, TOKEN_ROW, now),
        )
