"""Sending daily output over a fake Zalo: each content goes once, unclear ones are held back."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from gui_san_luong.domain.send_window import FREE_WINDOW_MS, SEND_WINDOW_MS
from gui_san_luong.services.output_dispatch import (
    RecipientPlan,
    RecipientState,
    SendOutcome,
    dispatch,
    load_day_sends,
    plan_recipients,
)
from gui_san_luong.services.worker_output import (
    EmployeeDay,
    OutputDetail,
    WorkerOutput,
    summarize_day,
)
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.zalo.client import BASE_URL, SEND_TEXT_PATH, ZaloClient
from gui_san_luong.zalo.schemas import MessageSource, ZaloMessage

DAY = date(2030, 1, 1)
NOW_MS = 1_900_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, UTC)
LINKS = {"1": "1001", "2": "1002", "3": "1003"}


def _employees(*codes: str, quantity: float = 10) -> list[EmployeeDay]:
    return summarize_day(
        [
            WorkerOutput(
                employee_code=code,
                employee_name=f"Người {code}",
                source="cat.xlsx",
                department="Cắt",
                role="NV01",
                process=None,
                day=DAY,
                quantity=quantity,
                unit="cái",
                base_quantity=quantity,
                detail=OutputDetail(item="Hàng X"),
            )
            for code in codes
        ],
        DAY,
    )


def _ok(message_id: str = "m") -> httpx.Response:
    return httpx.Response(200, json={"data": {"message_id": message_id}, "error": 0})


def _refused(code: int) -> httpx.Response:
    return httpx.Response(200, json={"error": code, "message": "x"})


@pytest.fixture
def zalo() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[ZaloClient]:
    with ZaloClient("fake-token") as zalo_client:
        yield zalo_client


def _wrote(*user_ids: str, age_ms: int = 60_000) -> MessageArchive:
    """Everyone named texted the OA a minute ago."""
    return MessageArchive(
        tuple(
            Conversation(
                user_id,
                None,
                (
                    ZaloMessage(
                        message_id=user_id,
                        src=MessageSource.FROM_USER,
                        time=NOW_MS - age_ms,
                        from_id=user_id,
                        to_id="oa",
                    ),
                ),
            )
            for user_id in user_ids
        )
    )


ACTIVE = _wrote(*LINKS.values())


def _plans(
    db: Path, employees: list[EmployeeDay], resend_unknown: bool = False
) -> list[RecipientPlan]:
    records = load_day_sends(db, DAY.isoformat())
    return plan_recipients(employees, LINKS, ACTIVE, records, NOW, resend_unknown)


def test_unlinked_people_are_skipped_and_linked_ones_are_ready(tmp_path: Path) -> None:
    plans = plan_recipients(_employees("1", "9"), LINKS, ACTIVE, [], NOW)

    assert [p.state for p in plans] == [RecipientState.READY, RecipientState.NOT_LINKED]
    assert plans[0].pending_parts == (1,)


def test_without_loaded_messages_nobody_is_sent_to() -> None:
    (plan,) = plan_recipients(_employees("1"), LINKS, None, [], NOW)

    assert plan.state is RecipientState.NO_INTERACTION


def test_someone_who_never_wrote_to_the_oa_is_not_sent_to() -> None:
    oa_only = MessageArchive(
        (
            Conversation(
                "1001",
                None,
                (
                    ZaloMessage(
                        message_id="x",
                        src=MessageSource.FROM_OA,
                        time=NOW_MS - 60_000,
                        from_id="oa",
                        to_id="1001",
                    ),
                ),
            ),
        )
    )
    plans = plan_recipients(_employees("1", "2"), LINKS, oa_only, [], NOW)

    assert [p.state for p in plans] == [
        RecipientState.NO_INTERACTION,
        RecipientState.NO_INTERACTION,
    ]


def test_the_same_content_is_never_sent_twice(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    route = zalo.post(SEND_TEXT_PATH).mock(return_value=_ok())
    employees = _employees("1", "2")

    results = dispatch(db, client, _plans(db, employees), "local")
    again = _plans(db, employees)

    assert [r.outcome for r in results] == [SendOutcome.SENT, SendOutcome.SENT]
    assert {p.state for p in again} == {RecipientState.ALREADY_SENT}
    assert dispatch(db, client, again, "local") == []
    assert route.call_count == 2


def test_changed_figures_are_sent_again(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(SEND_TEXT_PATH).mock(return_value=_ok())
    dispatch(db, client, _plans(db, _employees("1")), "local")

    (plan,) = _plans(db, _employees("1", quantity=12))

    assert plan.state is RecipientState.READY


def test_an_unknown_outcome_is_held_back_unless_asked(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(SEND_TEXT_PATH).mock(side_effect=httpx.ReadTimeout("slow"))

    (result,) = dispatch(db, client, _plans(db, _employees("1")), "local")

    assert result.outcome is SendOutcome.UNKNOWN
    (held,) = _plans(db, _employees("1"))
    assert held.state is RecipientState.UNKNOWN_BEFORE
    (forced,) = _plans(db, _employees("1"), resend_unknown=True)
    assert forced.state is RecipientState.READY


def test_a_failed_send_can_be_retried(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(SEND_TEXT_PATH).mock(return_value=_refused(-230))

    (result,) = dispatch(db, client, _plans(db, _employees("1")), "local")

    assert (result.outcome, result.error_code) == (SendOutcome.FAILED, -230)
    assert _plans(db, _employees("1"))[0].state is RecipientState.READY


def test_a_token_error_stops_the_batch(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    route = zalo.post(SEND_TEXT_PATH).mock(return_value=_refused(-216))

    results = dispatch(db, client, _plans(db, _employees("1", "2", "3")), "local")

    assert [r.outcome for r in results] == [
        SendOutcome.FAILED,
        SendOutcome.STOPPED,
        SendOutcome.STOPPED,
    ]
    assert route.call_count == 1


def test_a_recipient_refusal_does_not_stop_the_others(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(SEND_TEXT_PATH).mock(side_effect=[_refused(-230), _ok(), _ok()])

    results = dispatch(db, client, _plans(db, _employees("1", "2", "3")), "local")

    assert [r.outcome for r in results] == [
        SendOutcome.FAILED,
        SendOutcome.SENT,
        SendOutcome.SENT,
    ]


def test_a_part_left_sending_by_a_crash_counts_as_unknown(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(SEND_TEXT_PATH).mock(return_value=_ok())
    dispatch(db, client, _plans(db, _employees("1")), "local")
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE output_sends SET status = 'sending'")

    (plan,) = _plans(db, _employees("1"))

    assert plan.state is RecipientState.UNKNOWN_BEFORE


def _archive(user_id: str, age_ms: int) -> MessageArchive:
    message = ZaloMessage(
        message_id="x",
        src=MessageSource.FROM_USER,
        time=NOW_MS - age_ms,
        from_id=user_id,
        to_id="oa",
    )
    return MessageArchive((Conversation(user_id, None, (message,)),))


def test_someone_silent_for_over_7_days_is_not_sent_to() -> None:
    (plan,) = plan_recipients(
        _employees("1"), LINKS, _archive("1001", SEND_WINDOW_MS + 1), [], NOW
    )

    assert plan.state is RecipientState.NEEDS_INTERACTION


def test_someone_active_in_48_hours_is_ready_and_free() -> None:
    (plan,) = plan_recipients(
        _employees("1"), LINKS, _archive("1001", FREE_WINDOW_MS - 1), [], NOW
    )

    assert plan.state is RecipientState.READY
    assert plan.window is not None and plan.window.value == "free"
    assert plan.last_interaction_ms == NOW_MS - FREE_WINDOW_MS + 1
