"""Reading OA messages against a fake Zalo: no request leaves this machine."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from gui_san_luong.services.zalo_messages import (
    fetch_all_messages,
    fetch_conversation,
)
from gui_san_luong.zalo.client import (
    BASE_URL,
    CONVERSATION_PATH,
    MAX_PAGE_SIZE,
    RECENT_CHATS_PATH,
    ZaloClient,
)
from gui_san_luong.zalo.errors import (
    ZaloAuthError,
    ZaloConfigurationError,
    ZaloRateLimitError,
    ZaloRequestError,
    ZaloUnavailableError,
)
from gui_san_luong.zalo.schemas import MessageSource

TOKEN = "fake-token"
OA_ID = "3120036654733951760"
USER_A = "2512523625412515"
USER_B = "3700646744485476903"


def _message(
    message_id: str, user_id: str, time: int, from_user: bool = True, **extra: Any
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "src": 1 if from_user else 0,
        "time": time,
        "type": "text",
        "message": f"tin {message_id}",
        # Zalo documents ids as numbers in the JSON.
        "from_id": int(user_id if from_user else OA_ID),
        "to_id": int(OA_ID if from_user else user_id),
        "from_display_name": "Người A" if from_user else "OA",
        "to_display_name": "OA" if from_user else "Người A",
        **extra,
    }


def _ok(data: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "error": 0, "message": "Success"})


def _sent_data(request: httpx.Request) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(request.url.params["data"])
    return result


def _paged(pages: dict[int, list[dict[str, Any]]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(pages.get(_sent_data(request)["offset"], []))

    return handler


@pytest.fixture
def zalo() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[ZaloClient]:
    with ZaloClient(TOKEN, sleep=lambda _: None) as zalo_client:
        yield zalo_client


def test_token_goes_in_the_header_and_data_as_json(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    route = zalo.get(CONVERSATION_PATH).mock(return_value=_ok([]))

    client.conversation(USER_A, offset=20)

    request = route.calls.last.request
    assert request.headers["access_token"] == TOKEN
    assert TOKEN not in str(request.url)
    assert _sent_data(request) == {"user_id": int(USER_A), "offset": 20, "count": 10}


def test_numeric_ids_are_kept_as_exact_text(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    zalo.get(RECENT_CHATS_PATH).mock(return_value=_ok([_message("m1", USER_B, 5)]))

    (message,) = client.recent_messages(0)

    assert message.from_id == USER_B
    assert message.user_id == USER_B
    assert message.src is MessageSource.FROM_USER


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (-216, ZaloAuthError),
        (-220, ZaloAuthError),
        (-32, ZaloRateLimitError),
        (-223, ZaloConfigurationError),
        (-201, ZaloRequestError),
    ],
)
def test_error_codes_become_exceptions_even_with_http_200(
    zalo: respx.MockRouter, client: ZaloClient, code: int, error: type[Exception]
) -> None:
    zalo.get(RECENT_CHATS_PATH).mock(
        return_value=httpx.Response(200, json={"error": code, "message": "x"})
    )

    with pytest.raises(error):
        client.recent_messages(0)


def test_server_errors_are_retried_then_reported(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    route = zalo.get(RECENT_CHATS_PATH).mock(return_value=httpx.Response(502))

    with pytest.raises(ZaloUnavailableError):
        client.recent_messages(0)
    assert route.call_count == 3


def test_a_retry_that_succeeds_returns_the_messages(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    zalo.get(RECENT_CHATS_PATH).mock(
        side_effect=[httpx.ConnectError("down"), _ok([_message("m1", USER_A, 1)])]
    )

    assert [m.message_id for m in client.recent_messages(0)] == ["m1"]


def test_a_conversation_is_read_page_by_page_until_a_short_page(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    first = [_message(f"a{i}", USER_A, 100 - i) for i in range(MAX_PAGE_SIZE)]
    second = [_message("a10", USER_A, 50, from_user=False)]
    route = zalo.get(CONVERSATION_PATH).mock(side_effect=_paged({0: first, 10: second}))

    conversation = fetch_conversation(client, USER_A)

    assert route.call_count == 2
    assert len(conversation.messages) == 11
    # Oldest first, as a chat reads.
    assert conversation.messages[0].message_id == "a10"
    assert conversation.display_name == "Người A"
    assert not conversation.truncated


def test_an_api_that_ignores_the_offset_does_not_loop_forever(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    page = [_message(f"a{i}", USER_A, i) for i in range(MAX_PAGE_SIZE)]
    route = zalo.get(CONVERSATION_PATH).mock(return_value=_ok(page))

    conversation = fetch_conversation(client, USER_A)

    assert route.call_count == 2
    assert len(conversation.messages) == MAX_PAGE_SIZE


def test_a_very_long_conversation_is_capped_and_flagged(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    def endless(request: httpx.Request) -> httpx.Response:
        offset = _sent_data(request)["offset"]
        return _ok([_message(f"a{offset + i}", USER_A, offset + i) for i in range(10)])

    zalo.get(CONVERSATION_PATH).mock(side_effect=endless)

    conversation = fetch_conversation(client, USER_A, max_pages=3)

    assert len(conversation.messages) == 30
    assert conversation.truncated


def test_all_messages_cover_recent_users_and_extra_ids(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    zalo.get(RECENT_CHATS_PATH).mock(
        return_value=_ok(
            [
                _message("b2", USER_B, 30),
                _message("a1", USER_A, 20, from_user=False),
            ]
        )
    )
    extra = "999"
    conversations = {
        USER_A: [_message("a1", USER_A, 20, from_user=False)],
        USER_B: [_message("b2", USER_B, 30), _message("b1", USER_B, 10)],
        extra: [_message("x1", extra, 1)],
    }

    def conversation(request: httpx.Request) -> httpx.Response:
        return _ok(conversations[str(_sent_data(request)["user_id"])])

    zalo.get(CONVERSATION_PATH).mock(side_effect=conversation)

    archive = fetch_all_messages(client, [extra, USER_A])

    # The OA's own reply still names the user it went to.
    assert [c.user_id for c in archive.conversations] == [USER_B, USER_A, extra]
    assert archive.message_count == 4


def test_an_empty_token_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError):
        ZaloClient("  ")


def test_reading_stops_at_messages_already_kept(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    def endless(request: httpx.Request) -> httpx.Response:
        offset = _sent_data(request)["offset"]
        # Newest first: a higher offset holds older messages.
        return _ok(
            [_message(f"a{offset + i}", USER_A, -(offset + i)) for i in range(10)]
        )

    route = zalo.get(CONVERSATION_PATH).mock(side_effect=endless)

    conversation = fetch_conversation(client, USER_A, known_ids=frozenset({"a15"}))

    # The second page holds a kept message, so the third is never asked for.
    assert route.call_count == 2
    assert len(conversation.messages) == 20
