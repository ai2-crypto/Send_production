"""Loaded messages survive a page refresh, and later loads add to them."""

from datetime import UTC, datetime
from pathlib import Path

from gui_san_luong.services.message_store import (
    forget_stored_messages,
    load_stored_archive,
    store_archive,
)
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.zalo.schemas import MessageSource, ZaloMessage

FIRST_LOAD = datetime(2030, 1, 1, 8, tzinfo=UTC)
SECOND_LOAD = datetime(2030, 1, 2, 8, tzinfo=UTC)


def _message(message_id: str, user_id: str, time_ms: int) -> ZaloMessage:
    return ZaloMessage(
        message_id=message_id,
        src=MessageSource.FROM_USER,
        time=time_ms,
        message=f"tin {message_id}",
        from_id=user_id,
        to_id="oa",
        url="https://example.invalid/a.jpg",
    )


def _archive(
    fetched_at: datetime, *conversations: tuple[str, str | None, list[ZaloMessage]]
) -> MessageArchive:
    return MessageArchive(
        tuple(Conversation(u, name, tuple(m)) for u, name, m in conversations),
        fetched_at,
    )


def test_nothing_stored_gives_no_archive(tmp_path: Path) -> None:
    assert load_stored_archive(tmp_path / "test.db") is None


def test_a_stored_load_comes_back_whole(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    archive = _archive(FIRST_LOAD, ("1", "Anh A", [_message("m1", "1", 10)]))

    store_archive(db, archive)
    restored = load_stored_archive(db)

    assert restored == archive


def test_loads_add_up_and_keep_people_zalo_no_longer_lists(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    store_archive(db, _archive(FIRST_LOAD, ("1", "Anh A", [_message("m1", "1", 10)])))

    merged = store_archive(
        db,
        _archive(
            SECOND_LOAD,
            ("2", "Chị B", [_message("m2", "2", 30)]),
            ("1", None, [_message("m1", "1", 10), _message("m3", "1", 20)]),
        ),
    )

    assert [c.user_id for c in merged.conversations] == ["2", "1"]
    person_a = merged.conversations[1]
    assert [m.message_id for m in person_a.messages] == ["m1", "m3"]
    # A load without a name keeps the one already known.
    assert person_a.display_name == "Anh A"
    assert merged.fetched_at == SECOND_LOAD


def test_forgetting_removes_every_stored_message(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    store_archive(db, _archive(FIRST_LOAD, ("1", "Anh A", [_message("m1", "1", 10)])))

    assert forget_stored_messages(db) == 1
    assert load_stored_archive(db) is None
