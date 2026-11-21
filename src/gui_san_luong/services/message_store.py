"""Keep the messages read from Zalo, so the page remembers who interacted after a refresh.

Loads add up: a message read once stays until the operator deletes the stored messages, so
people who wrote long ago are not forgotten when Zalo's recent list moves on.
"""

import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from gui_san_luong.db.connection import open_database
from gui_san_luong.db.messages import (
    MessageRecord,
    delete_messages,
    fetch_messages,
    upsert_messages,
)
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.zalo.schemas import ZaloMessage

STORAGE_ERROR = "Không đọc hoặc ghi được tin nhắn đã lưu. Hãy thử lại."


class MessageStorageError(Exception):
    """A user-facing reason why stored messages could not be read or written."""


def _records(archive: MessageArchive) -> list[MessageRecord]:
    fetched_at = archive.fetched_at.astimezone(UTC).isoformat()
    return [
        MessageRecord(
            message.message_id,
            conversation.user_id,
            int(message.src),
            message.time,
            conversation.display_name,
            message.model_dump_json(),
            fetched_at,
        )
        for conversation in archive.conversations
        for message in conversation.messages
    ]


def _archive(records: list[MessageRecord]) -> MessageArchive | None:
    if not records:
        return None
    messages: defaultdict[str, list[ZaloMessage]] = defaultdict(list)
    names: dict[str, str] = {}
    for record in records:
        try:
            message = ZaloMessage.model_validate_json(record.message_json)
        except ValidationError:
            # A row written by an older version that no longer parses is skipped, not fatal.
            continue
        messages[record.zalo_user_id].append(message)
        if record.user_display_name:
            names[record.zalo_user_id] = record.user_display_name
    conversations = [
        Conversation(
            user_id,
            names.get(user_id),
            tuple(sorted(items, key=lambda message: message.time)),
        )
        for user_id, items in messages.items()
    ]
    conversations.sort(key=lambda c: c.messages[-1].time, reverse=True)
    fetched_at = max(datetime.fromisoformat(record.fetched_at) for record in records)
    return MessageArchive(tuple(conversations), fetched_at)


def load_stored_archive(db_path: Path) -> MessageArchive | None:
    """Every message kept so far, or None when nothing was ever loaded."""
    try:
        with open_database(db_path) as connection:
            return _archive(fetch_messages(connection))
    except sqlite3.Error as error:
        raise MessageStorageError(STORAGE_ERROR) from error


def store_archive(db_path: Path, archive: MessageArchive) -> MessageArchive:
    """Add a fresh load to what is kept, and return everything kept."""
    try:
        with open_database(db_path) as connection:
            upsert_messages(connection, _records(archive))
            return _archive(fetch_messages(connection)) or archive
    except sqlite3.Error as error:
        raise MessageStorageError(STORAGE_ERROR) from error


def forget_stored_messages(db_path: Path) -> int:
    try:
        with open_database(db_path) as connection:
            return delete_messages(connection)
    except sqlite3.Error as error:
        raise MessageStorageError(STORAGE_ERROR) from error
