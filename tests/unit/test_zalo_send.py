"""Sending a test message against a fake Zalo: no message is ever really sent."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from gui_san_luong.domain.send_window import (
    FREE_WINDOW_MS,
    SEND_WINDOW_MS,
    Eligibility,
    classify_eligibility,
)
from gui_san_luong.services.zalo_send import (
    eligibility,
    last_interaction_ms,
    send_test_message,
)
from gui_san_luong.zalo.client import (
    BASE_URL,
    MAX_TEXT_LENGTH,
    SEND_TEXT_PATH,
    ZaloClient,
)
from gui_san_luong.zalo.schemas import MessageSource, SendStatus, ZaloMessage

TOKEN = "fake-token"
USER = "2512523625412515"
NOW_MS = 1_800_000_000_000


@pytest.fixture
def zalo() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[ZaloClient]:
    with ZaloClient(TOKEN) as zalo_client:
        yield zalo_client


def _sent(message_id: str = "m1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "quota": {"quota_type": "reply", "remain": "8"},
                "message_id": message_id,
                "user_id": USER,
                "sent_time": "1626926349402",
            },
            "error": 0,
            "message": "Success",
        },
    )


def test_a_sent_message_returns_its_id_and_posts_the_documented_body(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    route = zalo.post(SEND_TEXT_PATH).mock(return_value=_sent("abc"))

    result = client.send_text(USER, "Xin chào")

    assert result.status is SendStatus.SENT
    assert result.message_id == "abc"
    assert result.quota_type == "reply"
    request = route.calls.last.request
    assert request.headers["access_token"] == TOKEN
    assert json.loads(request.content) == {
        "recipient": {"user_id": USER},
        "message": {"text": "Xin chào"},
    }


def test_a_refusal_is_a_failure_with_its_code(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    zalo.post(SEND_TEXT_PATH).mock(
        return_value=httpx.Response(200, json={"error": -230, "message": "x"})
    )

    result = client.send_text(USER, "Xin chào")

    assert result.status is SendStatus.FAILED
    assert result.error_code == -230


def test_a_connection_that_never_opened_is_a_safe_failure(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    zalo.post(SEND_TEXT_PATH).mock(side_effect=httpx.ConnectError("down"))

    assert client.send_text(USER, "Xin chào").status is SendStatus.FAILED


@pytest.mark.parametrize(
    "response",
    [
        httpx.ReadTimeout("slow"),
        httpx.Response(502),
        httpx.Response(200, text="not json"),
    ],
)
def test_an_unclear_outcome_is_unknown_and_not_retried(
    zalo: respx.MockRouter, client: ZaloClient, response: object
) -> None:
    route = zalo.post(SEND_TEXT_PATH)
    if isinstance(response, Exception):
        route.mock(side_effect=response)
    else:
        assert isinstance(response, httpx.Response)
        route.mock(return_value=response)

    assert client.send_text(USER, "Xin chào").status is SendStatus.UNKNOWN
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("user_id", "text"),
    [("abc", "Xin chào"), (USER, "   "), (USER, "x" * (MAX_TEXT_LENGTH + 1))],
)
def test_invalid_input_is_refused_before_any_call(
    zalo: respx.MockRouter, client: ZaloClient, user_id: str, text: str
) -> None:
    route = zalo.post(SEND_TEXT_PATH).mock(return_value=_sent())

    with pytest.raises(ValueError):
        client.send_text(user_id, text)
    assert route.call_count == 0


def test_every_send_is_recorded_in_the_audit_log(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(SEND_TEXT_PATH).mock(return_value=_sent("abc"))

    sent = send_test_message(db, client, USER, "Xin chào", "local")

    assert not sent.audit_failed
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT actor, action, target FROM audit_log"
        ).fetchall()
    assert rows == [("local", "zalo_test_message:sent", f"{USER}:abc")]


@pytest.mark.parametrize(
    ("age_ms", "expected"),
    [
        (0, Eligibility.FREE),
        (FREE_WINDOW_MS - 1, Eligibility.FREE),
        (FREE_WINDOW_MS, Eligibility.PAID),
        (SEND_WINDOW_MS, Eligibility.PAID),
        (SEND_WINDOW_MS + 1, Eligibility.NEEDS_INTERACTION),
    ],
)
def test_send_window_boundaries(age_ms: int, expected: Eligibility) -> None:
    assert classify_eligibility(NOW_MS - age_ms, NOW_MS) is expected


def test_no_interaction_at_all_needs_one_first() -> None:
    assert classify_eligibility(None, NOW_MS) is Eligibility.NEEDS_INTERACTION


def _message(src: MessageSource, time_ms: int) -> ZaloMessage:
    return ZaloMessage(
        message_id=str(time_ms), src=src, time=time_ms, from_id="1", to_id="2"
    )


def test_only_the_users_own_messages_count_as_interaction() -> None:
    now = datetime.fromtimestamp(NOW_MS / 1000, UTC)
    messages = [
        _message(MessageSource.FROM_USER, NOW_MS - SEND_WINDOW_MS - 1),
        _message(MessageSource.FROM_OA, NOW_MS - 1),
    ]

    assert last_interaction_ms(messages) == NOW_MS - SEND_WINDOW_MS - 1
    assert eligibility(messages, now) is Eligibility.NEEDS_INTERACTION
