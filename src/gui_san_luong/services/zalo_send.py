"""Send one test message from the OA and record the attempt.

This is the test page's direct send, for checking the OA can reach a person. Production
reports will go through a queue and the worker instead, so a crash mid-batch cannot lose
track of which messages may already have been delivered.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gui_san_luong.db.audit_log import append_audit
from gui_san_luong.db.connection import open_database
from gui_san_luong.domain.send_window import Eligibility, classify_eligibility
from gui_san_luong.zalo.client import ZaloClient
from gui_san_luong.zalo.schemas import MessageSource, SendResult, ZaloMessage

ACTION_PREFIX = "zalo_test_message"


@dataclass(frozen=True)
class SentTestMessage:
    result: SendResult
    # The message left, but recording it failed; it must not be sent again blindly.
    audit_failed: bool = False


def last_interaction_ms(messages: Iterable[ZaloMessage]) -> int | None:
    """The user's last message to the OA; the OA's own messages do not count."""
    return max(
        (m.time for m in messages if m.src is MessageSource.FROM_USER), default=None
    )


def eligibility(messages: Iterable[ZaloMessage], now: datetime) -> Eligibility:
    now_ms = int(now.timestamp() * 1000)
    return classify_eligibility(last_interaction_ms(messages), now_ms)


def send_test_message(
    db_path: Path, client: ZaloClient, user_id: str, text: str, actor: str
) -> SentTestMessage:
    result = client.send_text(user_id, text)
    target = f"{user_id}:{result.message_id or result.error_code or ''}"
    try:
        with open_database(db_path) as connection:
            append_audit(
                connection,
                actor,
                f"{ACTION_PREFIX}:{result.status}",
                target,
                datetime.now(UTC).isoformat(),
            )
    except sqlite3.Error:
        return SentTestMessage(result, audit_failed=True)
    return SentTestMessage(result)
