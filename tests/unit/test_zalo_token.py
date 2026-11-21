"""Refreshing the OA token pair against a fake Zalo: no real token is ever used."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from gui_san_luong.db.connection import open_database
from gui_san_luong.db.oauth_tokens import claim_refresh
from gui_san_luong.services.zalo_token import (
    LEASE_DURATION,
    REFRESH_MARGIN,
    RETRY_DELAY,
    TokenSettings,
    TokenSetupError,
    current_access_token,
    forget_tokens,
    refresh_now,
    save_pasted_tokens,
    token_status,
)
from gui_san_luong.zalo.client import OAUTH_TOKEN_URL, refresh_oa_token
from gui_san_luong.zalo.errors import (
    ZaloRefreshRejectedError,
    ZaloUnavailableError,
)
from gui_san_luong.zalo.schemas import TokenPair

APP_ID = "1234567890"
SECRET = "fake-secret"
START = datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
SETTINGS = TokenSettings(APP_ID, SECRET, Fernet.generate_key().decode())


# ---------------------------------------------------------------- the HTTP call


@pytest.fixture
def oauth() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def test_refresh_sends_the_documented_request_and_reads_string_expiry(
    oauth: respx.MockRouter,
) -> None:
    route = oauth.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "A2", "refresh_token": "R2", "expires_in": "90000"},
        )
    )
    pair = refresh_oa_token(APP_ID, SECRET, "R1")
    assert (pair.access_token, pair.refresh_token, pair.expires_in) == (
        "A2",
        "R2",
        90000,
    )
    request = route.calls.last.request
    assert request.headers["secret_key"] == SECRET
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.content.decode()) == {
        "refresh_token": ["R1"],
        "app_id": [APP_ID],
        "grant_type": ["refresh_token"],
    }


def test_an_answer_without_tokens_is_a_rejection_that_never_shows_the_token(
    oauth: respx.MockRouter,
) -> None:
    oauth.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "error": -14014,
                "error_name": "Invalid refresh token",
                "error_description": "Invalid refresh token",
            },
        )
    )
    with pytest.raises(ZaloRefreshRejectedError) as raised:
        refresh_oa_token(APP_ID, SECRET, "secret-refresh-token")
    assert raised.value.code == -14014
    assert raised.value.message == "Invalid refresh token"
    assert "secret-refresh-token" not in str(raised.value)


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(502), httpx.ConnectError("down"), httpx.ReadTimeout("slow")],
)
def test_network_and_server_failures_are_unavailable(
    oauth: respx.MockRouter, answer: httpx.Response | Exception
) -> None:
    route = oauth.post(OAUTH_TOKEN_URL)
    if isinstance(answer, Exception):
        route.mock(side_effect=answer)
    else:
        route.mock(return_value=answer)
    with pytest.raises(ZaloUnavailableError):
        refresh_oa_token(APP_ID, SECRET, "R1")
    # Never retried: a request that reached Zalo has already spent the token.
    assert route.call_count == 1


# ---------------------------------------------------------------- storing and refreshing


class FakeZalo:
    """Issues numbered pairs and remembers every refresh token it was given."""

    def __init__(self) -> None:
        self.used: list[str] = []
        self.issued = 1
        self.failure: Exception | None = None

    def __call__(self, app_id: str, secret_key: str, refresh_token: str) -> TokenPair:
        assert (app_id, secret_key) == (APP_ID, SECRET)
        self.used.append(refresh_token)
        if self.failure:
            raise self.failure
        self.issued += 1
        return TokenPair(
            access_token=f"A{self.issued}",
            refresh_token=f"R{self.issued}",
            expires_in=90000,
        )


class Clock:
    def __init__(self, moment: datetime = START) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def sleep(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.db"
    save_pasted_tokens(path, SETTINGS, "A1", "R1", START)
    return path


def _token(db: Path, zalo: FakeZalo, clock: Clock) -> str | None:
    return current_access_token(db, SETTINGS, clock, zalo, clock.sleep)


def test_pasted_tokens_are_refreshed_at_first_use_and_stored(db: Path) -> None:
    zalo, clock = FakeZalo(), Clock()
    assert _token(db, zalo, clock) == "A2"
    assert zalo.used == ["R1"]
    status = token_status(db)
    assert status is not None
    assert status.access_expires_at == START + timedelta(seconds=90000)
    assert status.last_error is None


def test_a_token_far_from_expiry_is_used_without_refreshing(db: Path) -> None:
    zalo, clock = FakeZalo(), Clock()
    _token(db, zalo, clock)
    clock.moment += timedelta(seconds=90000) - REFRESH_MARGIN - timedelta(minutes=1)
    assert _token(db, zalo, clock) == "A2"
    assert zalo.used == ["R1"]


def test_each_refresh_uses_the_newest_refresh_token_never_an_old_one(db: Path) -> None:
    zalo, clock = FakeZalo(), Clock()
    for _ in range(3):
        _token(db, zalo, clock)
        clock.moment += timedelta(hours=25)
    assert zalo.used == ["R1", "R2", "R3"]


def test_tokens_are_never_stored_in_plain_text(db: Path) -> None:
    _token(db, FakeZalo(), Clock())
    dump = "\n".join(sqlite3.connect(db).iterdump())
    for plain in ("A1", "R1", "A2", "R2"):
        assert f"'{plain}'" not in dump


def test_a_rejected_refresh_token_asks_for_new_tokens_and_is_not_tried_again(
    db: Path,
) -> None:
    zalo, clock = FakeZalo(), Clock()
    zalo.failure = ZaloRefreshRejectedError(-14014, "Invalid refresh token")
    # The stored token is still handed out; Zalo will say if it no longer works.
    assert _token(db, zalo, clock) == "A1"
    status = token_status(db)
    assert status is not None and status.needs_reauthorization
    assert status.last_error and "Invalid refresh token" in status.last_error
    _token(db, zalo, clock)
    assert zalo.used == ["R1"]
    # Pasting new tokens clears the alarm.
    save_pasted_tokens(db, SETTINGS, "A9", "R9", clock())
    status = token_status(db)
    assert status is not None and not status.needs_reauthorization


def test_an_unreachable_zalo_is_retried_at_the_next_use(db: Path) -> None:
    zalo, clock = FakeZalo(), Clock()
    zalo.failure = ZaloUnavailableError(None, "network error")
    assert _token(db, zalo, clock) == "A1"
    status = token_status(db)
    assert status is not None and not status.needs_reauthorization
    assert status.last_error
    zalo.failure = None
    # Not before the retry delay, so a failing Zalo is not called on every page load.
    assert _token(db, zalo, clock) == "A1"
    assert zalo.used == ["R1"]
    clock.moment += RETRY_DELAY
    assert _token(db, zalo, clock) == "A2"
    assert zalo.used == ["R1", "R1"]
    status = token_status(db)
    assert status is not None and status.last_error is None


def test_a_wrong_secret_key_keeps_the_tokens_and_refresh_now_retries_at_once(
    db: Path,
) -> None:
    zalo, clock = FakeZalo(), Clock()
    zalo.failure = ZaloRefreshRejectedError(-14004, "Invalid secret key")
    assert _token(db, zalo, clock) == "A1"
    status = token_status(db)
    assert status is not None and not status.needs_reauthorization
    assert status.last_error and "ZALO_SECRET_KEY" in status.last_error
    # The operator fixes .env and presses "refresh now", without new tokens.
    zalo.failure = None
    assert refresh_now(db, SETTINGS, clock, zalo, clock.sleep) == "A2"
    assert zalo.used == ["R1", "R1"]


def test_a_caller_waits_for_a_refresh_already_running_instead_of_starting_one(
    db: Path,
) -> None:
    zalo, clock = FakeZalo(), Clock()
    with open_database(db) as connection:
        assert claim_refresh(
            connection, START.isoformat(), (START + LEASE_DURATION).isoformat()
        )

    def other_caller_finishes(seconds: float) -> None:
        clock.sleep(seconds)
        # Meanwhile the lease holder stores the pair it got from Zalo.
        save_pasted_tokens(db, SETTINGS, "A-other", "R-other", clock())

    token = current_access_token(db, SETTINGS, clock, zalo, other_caller_finishes)
    assert token == "A-other"
    assert zalo.used == []


def test_a_lease_left_by_a_crashed_caller_runs_out(db: Path) -> None:
    zalo, clock = FakeZalo(), Clock()
    with open_database(db) as connection:
        claim_refresh(
            connection, START.isoformat(), (START + LEASE_DURATION).isoformat()
        )
    # Waits out the lease, then hands out the stored token rather than failing.
    assert _token(db, zalo, clock) == "A1"
    clock.moment += LEASE_DURATION
    assert _token(db, zalo, clock) == "A2"


def test_missing_settings_say_which_variables_to_set(db: Path) -> None:
    settings = TokenSettings(APP_ID, "", SETTINGS.encryption_key)
    clock = Clock()
    with pytest.raises(TokenSetupError, match="ZALO_SECRET_KEY"):
        refresh_now(db, settings, clock, FakeZalo(), clock.sleep)


def test_a_changed_encryption_key_asks_to_paste_again(db: Path) -> None:
    other = TokenSettings(APP_ID, SECRET, Fernet.generate_key().decode())
    with pytest.raises(TokenSetupError, match="TOKEN_ENCRYPTION_KEY"):
        current_access_token(db, other, Clock(), FakeZalo())


def test_no_stored_tokens_means_none(tmp_path: Path) -> None:
    assert current_access_token(tmp_path / "empty.db", SETTINGS, Clock()) is None


def test_forgotten_tokens_are_gone(db: Path) -> None:
    forget_tokens(db, START)
    assert token_status(db) is None


def test_a_pasted_token_has_an_unknown_expiry_until_the_first_refresh(db: Path) -> None:
    status = token_status(db)
    assert status is not None and not status.expiry_known
    _token(db, FakeZalo(), Clock())
    status = token_status(db)
    assert status is not None and status.expiry_known


def test_a_rejection_message_ending_in_a_period_is_not_doubled(db: Path) -> None:
    zalo, clock = FakeZalo(), Clock()
    zalo.failure = ZaloRefreshRejectedError(-14014, "Invalid refresh token.")
    _token(db, zalo, clock)
    status = token_status(db)
    assert status is not None and status.last_error
    assert "Invalid refresh token. " in status.last_error
    assert ".." not in status.last_error
