"""Collect every message between the OA and its users, ten at a time.

Zalo has no "list conversations" call for messages: the recent messages name the users who
wrote lately, and each user's conversation is then read page by page. Page caps keep one bad
answer (an API that ignores the offset, say) from looping forever.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from gui_san_luong.zalo.client import MAX_PAGE_SIZE, ZaloClient
from gui_san_luong.zalo.schemas import ZaloMessage

MAX_RECENT_PAGES = 50
MAX_CONVERSATION_PAGES = 100

Progress = Callable[[str], None]


@dataclass(frozen=True)
class Conversation:
    user_id: str
    display_name: str | None
    # Oldest first, as a chat reads.
    messages: tuple[ZaloMessage, ...]
    # The page cap was hit, so older messages may exist.
    truncated: bool = False


@dataclass(frozen=True)
class MessageArchive:
    conversations: tuple[Conversation, ...]
    # When the messages were read: anything sent after is not in the archive.
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def message_count(self) -> int:
        return sum(len(conversation.messages) for conversation in self.conversations)


def _read_pages(
    fetch_page: Callable[[int], list[ZaloMessage]],
    max_pages: int,
    known_ids: frozenset[str] = frozenset(),
) -> tuple[list[ZaloMessage], bool]:
    """Read newest first until a short or repeated page, or one holding a message already kept.

    Older pages hold only older messages, so once a kept message shows up everything beyond it
    is kept already. Returns the messages and whether the page cap was hit.
    """
    seen: set[str] = set()
    messages: list[ZaloMessage] = []
    for page in range(max_pages):
        batch = fetch_page(page * MAX_PAGE_SIZE)
        fresh = [message for message in batch if message.message_id not in seen]
        seen.update(message.message_id for message in fresh)
        messages.extend(fresh)
        reached_known = any(message.message_id in known_ids for message in batch)
        if len(batch) < MAX_PAGE_SIZE or not fresh or reached_known:
            return messages, False
    return messages, True


def _display_name(messages: Iterable[ZaloMessage]) -> str | None:
    # The newest name is the one the user goes by now.
    for message in sorted(messages, key=lambda item: item.time, reverse=True):
        if message.user_display_name:
            return message.user_display_name
    return None


def fetch_conversation(
    client: ZaloClient,
    user_id: str,
    max_pages: int = MAX_CONVERSATION_PAGES,
    known_ids: frozenset[str] = frozenset(),
) -> Conversation:
    messages, truncated = _read_pages(
        lambda offset: client.conversation(user_id, offset), max_pages, known_ids
    )
    return Conversation(
        user_id,
        _display_name(messages),
        tuple(sorted(messages, key=lambda message: message.time)),
        truncated,
    )


def recent_user_ids(
    client: ZaloClient,
    max_pages: int = MAX_RECENT_PAGES,
    known_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """Users with recent messages, the most recently active first."""
    messages, _ = _read_pages(client.recent_messages, max_pages, known_ids)
    user_ids: dict[str, None] = {}
    for message in sorted(messages, key=lambda item: item.time, reverse=True):
        user_ids.setdefault(message.user_id, None)
    return list(user_ids)


def fetch_all_messages(
    client: ZaloClient,
    extra_user_ids: Iterable[str] = (),
    progress: Progress | None = None,
    known_ids: frozenset[str] = frozenset(),
) -> MessageArchive:
    """Every conversation found through recent messages, plus users named by the caller.

    Messages in known_ids are kept already, so reading stops where they begin.
    """
    report = progress or (lambda _: None)
    report("Đang lấy danh sách tin nhắn gần nhất…")
    user_ids = dict.fromkeys(recent_user_ids(client, known_ids=known_ids))
    user_ids.update(dict.fromkeys(extra_user_ids))
    conversations = []
    for index, user_id in enumerate(user_ids, start=1):
        report(f"Đang lấy hội thoại {index}/{len(user_ids)}…")
        conversations.append(fetch_conversation(client, user_id, known_ids=known_ids))
    conversations.sort(
        key=lambda conversation: max(
            (message.time for message in conversation.messages), default=0
        ),
        reverse=True,
    )
    return MessageArchive(tuple(conversations))
