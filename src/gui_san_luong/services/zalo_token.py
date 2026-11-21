"""Keep the OA access token valid by trading the refresh token before the access token expires.

The operator pastes an access and refresh token from Zalo's API Explorer once. From then on the
app refreshes the pair itself, at the first use within REFRESH_MARGIN of expiry.

A refresh token works once, and the old access token dies as the new one is issued, so:
- only one caller refreshes at a time: a lease in the database, taken with a single UPDATE,
  makes everyone else wait and then read the new pair;
- the new pair is stored the moment Zalo returns it, before anything else, in one transaction;
- no transaction is open during the network call.

The refresh runs in the app rather than in a separate worker (decided with the user on
2026-11-19): the app is a single container, and the lease keeps refreshes one at a time.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from gui_san_luong.config import (
    token_encryption_key,
    zalo_access_token,
    zalo_app_id,
    zalo_secret_key,
)
from gui_san_luong.db.connection import open_database
from gui_san_luong.db.oauth_tokens import (
    ACTION_TOKENS_REFRESHED,
    ACTION_TOKENS_SET,
    TokenRecord,
    claim_refresh,
    delete_tokens,
    fetch_tokens,
    record_failure,
    save_tokens,
)
from gui_san_luong.zalo.client import refresh_oa_token
from gui_san_luong.zalo.errors import (
    ZaloError,
    ZaloOAuthErrorCode,
    ZaloRefreshRejectedError,
    ZaloUnavailableError,
)
from gui_san_luong.zalo.schemas import TokenPair

logger = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(hours=1)
# Zalo documents the refresh token as valid for three months.
REFRESH_TOKEN_LIFETIME = timedelta(days=90)
# Longer than the HTTP timeout, so a lease never runs out while its holder is still waiting.
LEASE_DURATION = timedelta(seconds=30)
WAIT_POLL_SECONDS = 0.5
# After a failure that may fix itself or needs a setting changed, the automatic refresh waits
# this long; the "refresh now" button does not.
RETRY_DELAY = timedelta(minutes=5)
# Rejections caused by the app's settings, not by the refresh token, which stays usable.
SETTINGS_REJECTIONS = frozenset({ZaloOAuthErrorCode.INVALID_SECRET_KEY})
SAVE_ATTEMPTS = 3
STORAGE_ERROR = "Không đọc hoặc ghi được token Zalo trong cơ sở dữ liệu."

type Refresher = Callable[[str, str, str], TokenPair]


class TokenSetupError(Exception):
    """A setting is missing or wrong; the message tells the operator what to fix."""


class TokenStorageError(Exception):
    pass


@dataclass(frozen=True)
class TokenSettings:
    app_id: str
    secret_key: str
    encryption_key: str

    @classmethod
    def from_environment(cls) -> TokenSettings:
        return cls(zalo_app_id(), zalo_secret_key(), token_encryption_key())

    def missing(self) -> list[str]:
        names = {
            "ZALO_APP_ID": self.app_id,
            "ZALO_SECRET_KEY": self.secret_key,
            "TOKEN_ENCRYPTION_KEY": self.encryption_key,
        }
        return [name for name, value in names.items() if not value.strip()]


@dataclass(frozen=True)
class TokenStatus:
    access_expires_at: datetime
    refresh_expires_at: datetime
    updated_at: datetime
    last_error: str | None
    needs_reauthorization: bool

    @property
    def expiry_known(self) -> bool:
        """False for a pasted token not yet refreshed: how long it has left is unknown."""
        # A pasted pair is stored as expiring a second after it was saved.
        return self.access_expires_at - self.updated_at > timedelta(minutes=1)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _fernet(settings: TokenSettings) -> Fernet:
    if not settings.encryption_key.strip():
        raise TokenSetupError(
            "Chưa có TOKEN_ENCRYPTION_KEY trong .env để mã hóa token khi lưu."
        )
    try:
        return Fernet(settings.encryption_key.strip().encode())
    except ValueError as error:
        raise TokenSetupError(
            "TOKEN_ENCRYPTION_KEY không hợp lệ: phải là khóa Fernet (44 ký tự)."
        ) from error


def _decrypt(fernet: Fernet, value: str) -> str:
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken as error:
        raise TokenSetupError(
            "Không giải mã được token đã lưu: TOKEN_ENCRYPTION_KEY đã bị đổi. Hãy dán lại "
            "token từ API Explorer."
        ) from error


def _read(path: Path) -> TokenRecord | None:
    try:
        with open_database(path) as connection:
            return fetch_tokens(connection)
    except sqlite3.Error as error:
        logger.warning("OA tokens could not be read", exc_info=error)
        raise TokenStorageError(STORAGE_ERROR) from error


def _store(
    path: Path, settings: TokenSettings, pair: TokenPair, now: datetime, action: str
) -> None:
    """Retried, because losing a freshly issued pair means asking for new tokens."""
    fernet = _fernet(settings)
    values = (
        fernet.encrypt(pair.access_token.encode()).decode(),
        fernet.encrypt(pair.refresh_token.encode()).decode(),
        _iso(now + timedelta(seconds=pair.expires_in)),
        _iso(now + REFRESH_TOKEN_LIFETIME),
        _iso(now),
        action,
    )
    for attempt in range(1, SAVE_ATTEMPTS + 1):
        try:
            with open_database(path) as connection:
                save_tokens(connection, *values)
            return
        except sqlite3.Error as error:
            logger.error(
                "New OA tokens could not be saved (attempt %d)", attempt, exc_info=error
            )
            if attempt == SAVE_ATTEMPTS:
                raise TokenStorageError(STORAGE_ERROR) from error
            time.sleep(WAIT_POLL_SECONDS)


def token_status(path: Path) -> TokenStatus | None:
    record = _read(path)
    if record is None:
        return None
    return TokenStatus(
        _parse(record.access_expires_at),
        _parse(record.refresh_expires_at),
        _parse(record.updated_at),
        record.last_error,
        record.needs_reauthorization,
    )


def save_pasted_tokens(
    path: Path,
    settings: TokenSettings,
    access_token: str,
    refresh_token: str,
    now: datetime,
) -> None:
    """Store tokens pasted from API Explorer.

    How long the pasted access token has left is unknown, so it is recorded as due for a
    refresh: the first use trades the refresh token at once, which also proves the app id and
    secret key are right. The pasted access token stays usable if that refresh fails.
    """
    if not access_token.strip() or not refresh_token.strip():
        raise TokenSetupError("Hãy dán cả access token và refresh token.")
    pair = TokenPair(
        access_token=access_token.strip(),
        refresh_token=refresh_token.strip(),
        expires_in=1,
    )
    _store(path, settings, pair, now - timedelta(seconds=1), ACTION_TOKENS_SET)


def forget_tokens(path: Path, now: datetime) -> None:
    try:
        with open_database(path) as connection:
            delete_tokens(connection, _iso(now))
    except sqlite3.Error as error:
        logger.warning("OA tokens could not be deleted", exc_info=error)
        raise TokenStorageError(STORAGE_ERROR) from error


def _record_failure(
    path: Path, error: str, needs_reauthorization: bool, retry_after: datetime
) -> None:
    try:
        with open_database(path) as connection:
            record_failure(connection, error, needs_reauthorization, _iso(retry_after))
    except sqlite3.Error as storage_error:
        # The lease then simply runs out; nothing is lost.
        logger.warning("Token refresh failure not recorded", exc_info=storage_error)


def _is_settings_rejection(error: ZaloError) -> bool:
    return (
        isinstance(error, ZaloRefreshRejectedError)
        and error.code in SETTINGS_REJECTIONS
    )


def _failure_text(error: ZaloError) -> str:
    if _is_settings_rejection(error):
        return (
            f"Zalo báo sai Secret key (mã lỗi {error.code}). Hãy kiểm tra ZALO_SECRET_KEY "
            "trong .env là khóa bí mật của đúng ứng dụng có ZALO_APP_ID (trang quản lý "
            "ứng dụng trên Zalo for Developers), không có dấu nháy hay khoảng trắng thừa, "
            "rồi bấm “Làm mới ngay”. Nếu sau đó Zalo báo refresh token không hợp lệ, "
            "lấy cặp token mới ở API Explorer."
        )
    if isinstance(error, ZaloRefreshRejectedError):
        code = f" (mã lỗi {error.code})" if error.code is not None else ""
        return (
            f"Zalo từ chối refresh token{code}: {error.message.rstrip('. ')}. Refresh "
            "token chỉ dùng được một lần: nó đã được dùng (kể cả bởi một lần làm mới lỗi "
            "trước đó, hoặc vì đã bấm “Lấy Access Token” thêm lần nữa trên API Explorer), "
            "đã hết hạn, hoặc ZALO_APP_ID / ZALO_SECRET_KEY sai. Hãy bấm “Lấy Access "
            "Token” trên API Explorer một lần rồi dán ngay cặp token mới."
        )
    return (
        "Không kết nối được Zalo để làm mới token; sẽ thử lại ở lần dùng sau. Nếu Zalo đã "
        "kịp cấp token mới mà mạng bị ngắt, refresh token cũ sẽ không dùng được nữa."
    )


def refresh_now(
    path: Path,
    settings: TokenSettings,
    now: Callable[[], datetime],
    refresher: Refresher = refresh_oa_token,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Refresh the pair, or wait for whoever is refreshing it, and return the access token."""
    if missing := settings.missing():
        raise TokenSetupError("Thiếu " + ", ".join(missing) + " trong .env.")
    fernet = _fernet(settings)
    before = _read(path)
    if before is None:
        raise TokenSetupError("Chưa có token: hãy dán token lấy từ API Explorer.")
    started = now()
    try:
        with open_database(path) as connection:
            claimed = claim_refresh(
                connection, _iso(started), _iso(started + LEASE_DURATION)
            )
    except sqlite3.Error as error:
        logger.warning("Token refresh lease not taken", exc_info=error)
        raise TokenStorageError(STORAGE_ERROR) from error
    if not claimed:
        return _await_other_refresh(path, fernet, before, started, now, sleep)

    refresh_token = _decrypt(fernet, before.refresh_token_enc)
    try:
        pair = refresher(settings.app_id, settings.secret_key, refresh_token)
    except (ZaloRefreshRejectedError, ZaloUnavailableError) as error:
        token_spent = isinstance(
            error, ZaloRefreshRejectedError
        ) and not _is_settings_rejection(error)
        logger.warning("OA token refresh failed: %s", error)
        _record_failure(path, _failure_text(error), token_spent, started + RETRY_DELAY)
        raise
    except BaseException:
        _record_failure(
            path, "Làm mới token bị gián đoạn.", False, started + RETRY_DELAY
        )
        raise
    _store(path, settings, pair, started, ACTION_TOKENS_REFRESHED)
    return pair.access_token


def _await_other_refresh(
    path: Path,
    fernet: Fernet,
    before: TokenRecord,
    started: datetime,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
) -> str:
    if before.needs_reauthorization:
        raise TokenSetupError(before.last_error or "Cần dán token mới từ API Explorer.")
    while now() < started + LEASE_DURATION:
        sleep(WAIT_POLL_SECONDS)
        record = _read(path)
        if record is None:
            raise TokenSetupError("Token vừa bị xóa.")
        if record.updated_at != before.updated_at:
            return _decrypt(fernet, record.access_token_enc)
        if record.last_error and record.last_error != before.last_error:
            break
    return _decrypt(fernet, before.access_token_enc)


def current_access_token(
    path: Path,
    settings: TokenSettings,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    refresher: Refresher = refresh_oa_token,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """The stored access token, refreshed first when it is within an hour of expiry.

    None when no token was ever pasted. When a refresh fails, the stored token is returned
    anyway: it may still work, and if not Zalo says so, while the failure is kept for the page.
    """
    record = _read(path)
    if record is None:
        return None
    fernet = _fernet(settings)
    expires_at = _parse(record.access_expires_at)
    waiting = record.retry_after is not None and now() < _parse(record.retry_after)
    if record.needs_reauthorization or waiting or expires_at - now() > REFRESH_MARGIN:
        return _decrypt(fernet, record.access_token_enc)
    try:
        return refresh_now(path, settings, now, refresher, sleep)
    except (ZaloError, TokenSetupError, TokenStorageError) as error:
        logger.warning(
            "Using the stored OA access token after a failed refresh: %s", error
        )
        return _decrypt(fernet, record.access_token_enc)


def resolve_access_token(path: Path) -> str:
    """The token to call Zalo with: the stored one, refreshed if due, else .env's.

    Empty when there is none. A setup or storage problem is not raised here: the token
    status says what is wrong, and callers only need to know whether they can call Zalo.
    """
    try:
        stored = current_access_token(path, TokenSettings.from_environment())
    except TokenSetupError, TokenStorageError:
        stored = None
    return stored or zalo_access_token()
