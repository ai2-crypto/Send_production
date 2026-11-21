"""Employee codes texted to the OA, proposed as Zalo links, and the 48-hour free window."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gui_san_luong.db.zalo_links import LinkRecord
from gui_san_luong.domain.send_window import FREE_WINDOW_MS
from gui_san_luong.services.worker_output import EmployeeIdentity
from gui_san_luong.services.zalo_codes import (
    CodeClaim,
    ProposalStatus,
    apply_proposals,
    find_code,
    free_window_users,
    latest_claims,
    plan_proposals,
)
from gui_san_luong.services.zalo_links import InvalidLinksError, load_links
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.zalo.schemas import MessageSource, ZaloMessage

NOW_MS = 1_800_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, UTC)
USER_A = "1111"
USER_B = "2222"


def _message(text: str, time_ms: int, src: MessageSource) -> ZaloMessage:
    return ZaloMessage(
        message_id=f"{text}-{time_ms}",
        src=src,
        time=time_ms,
        message=text,
        from_id="1",
        to_id="2",
    )


def _conversation(
    user_id: str, *messages: tuple[str, int, MessageSource]
) -> Conversation:
    return Conversation(
        user_id,
        f"Zalo {user_id}",
        tuple(_message(text, time, src) for text, time, src in messages),
    )


USER = MessageSource.FROM_USER
OA = MessageSource.FROM_OA


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Mã NV của em: 000020", "000020"),
        ("002960", "002960"),
        ("mã 002960, nhắc lại 002960", "002960"),
        ("Tháng 10 làm 1200000 mét", None),
        ("mã 12345", None),
        ("000020 hay 000021?", None),
        (None, None),
    ],
)
def test_a_code_is_six_digits_alone_and_unambiguous(
    text: str | None, code: str | None
) -> None:
    assert find_code(text) == code


def test_the_newest_users_message_with_a_code_wins() -> None:
    archive = MessageArchive(
        (
            _conversation(
                USER_A,
                ("000020", 1, USER),
                ("nhầm, mã đúng là 000021", 3, USER),
                ("OA hỏi lại: 000099", 4, OA),
                ("cảm ơn", 5, USER),
            ),
            _conversation(USER_B, ("xin chào", 2, USER)),
        )
    )

    (claim,) = latest_claims(archive)

    assert (claim.user_id, claim.code, claim.sent_ms) == (USER_A, "000021", 3)


def _claim(user_id: str, code: str) -> CodeClaim:
    return CodeClaim(user_id, f"Zalo {user_id}", code, 1, code)


ROSTER = [
    EmployeeIdentity("20", "Nguyễn Văn A", ("Dệt",)),
    EmployeeIdentity("002960", "Trần Thị B", ("May",)),
]


def test_a_padded_code_matches_how_the_files_write_it() -> None:
    (proposal,) = plan_proposals([_claim(USER_A, "000020")], [], ROSTER)

    assert proposal.employee_code == "20"
    assert proposal.employee_name == "Nguyễn Văn A"
    assert proposal.status is ProposalStatus.NEW
    assert proposal.selected_by_default


@pytest.mark.parametrize(
    ("stored", "claims", "status"),
    [
        (
            [LinkRecord("20", "A", USER_A)],
            [(USER_A, "000020")],
            ProposalStatus.UNCHANGED,
        ),
        (
            [LinkRecord("20", "A", USER_B)],
            [(USER_A, "000020")],
            ProposalStatus.REPLACES_ACCOUNT,
        ),
        (
            [LinkRecord("002960", "B", USER_A)],
            [(USER_A, "000020")],
            ProposalStatus.MOVES_ACCOUNT,
        ),
        (
            [],
            [(USER_A, "000020"), (USER_B, "000020")],
            ProposalStatus.CLAIMED_BY_SEVERAL,
        ),
        ([], [(USER_A, "123456")], ProposalStatus.NOT_IN_LIST),
    ],
)
def test_anything_that_could_link_the_wrong_person_is_not_preselected(
    stored: list[LinkRecord],
    claims: list[tuple[str, str]],
    status: ProposalStatus,
) -> None:
    proposals = plan_proposals([_claim(u, c) for u, c in claims], stored, ROSTER)

    assert proposals[0].status is status
    assert not proposals[0].selected_by_default


def test_without_an_employee_list_nothing_is_preselected() -> None:
    (proposal,) = plan_proposals([_claim(USER_A, "000020")], [], [])

    assert proposal.status is ProposalStatus.NO_LIST
    assert proposal.employee_code == "000020"
    assert not proposal.selected_by_default


def test_applying_keeps_other_links_and_moves_an_account(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    apply_proposals(
        db,
        [],
        plan_proposals([_claim(USER_B, "002960")], [], ROSTER),
        "local",
    )
    stored = load_links(db)
    (moved,) = plan_proposals([_claim(USER_B, "000020")], stored, ROSTER)

    apply_proposals(db, stored, [moved], "local")

    assert load_links(db) == [LinkRecord("20", "Nguyễn Văn A", USER_B)]
    with sqlite3.connect(db) as connection:
        actions = [row[0] for row in connection.execute("SELECT action FROM audit_log")]
    assert sorted(actions) == ["zalo_link_removed", "zalo_link_set", "zalo_link_set"]


def test_two_accounts_for_one_code_are_refused_whole(tmp_path: Path) -> None:
    proposals = plan_proposals(
        [_claim(USER_A, "000020"), _claim(USER_B, "000020")], [], ROSTER
    )

    with pytest.raises(InvalidLinksError):
        apply_proposals(tmp_path / "test.db", [], proposals, "local")


def test_only_users_who_wrote_within_48_hours_are_free() -> None:
    archive = MessageArchive(
        (
            _conversation(USER_A, ("hi", NOW_MS - FREE_WINDOW_MS + 60_000, USER)),
            _conversation(USER_B, ("hi", NOW_MS - FREE_WINDOW_MS, USER)),
            _conversation("3333", ("OA nhắn", NOW_MS - 1, OA)),
        )
    )

    (user,) = free_window_users(archive, NOW)

    assert user.user_id == USER_A
    assert user.free_until_ms == NOW_MS + 60_000
