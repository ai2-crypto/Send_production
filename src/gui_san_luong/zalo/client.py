"""The only place that sends HTTP requests to Zalo.

Read calls are retried on network and server errors, since reading twice does no harm.
A send is never retried here: Zalo may already have delivered it, and a message cannot be
recalled, so an unclear outcome is reported as unknown for a person to decide.
Requests are never logged: the access_token header would end up in the log.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Self

import httpx
from pydantic import ValidationError

from gui_san_luong.zalo.errors import (
    ZaloError,
    ZaloRefreshRejectedError,
    ZaloUnavailableError,
    error_for,
)
from gui_san_luong.zalo.schemas import SendResult, SendStatus, TokenPair, ZaloMessage

BASE_URL = "https://openapi.zalo.me"
OAUTH_TOKEN_URL = "https://oauth.zaloapp.com/v4/oa/access_token"
# Fields of an OAuth error answer worth showing; the documentation does not list them, so any
# that are present are used. None of them carries a token.
OAUTH_ERROR_FIELDS = ("error_name", "error_reason", "error_description", "message")
RECENT_CHATS_PATH = "/v2.0/oa/listrecentchat"
CONVERSATION_PATH = "/v2.0/oa/conversation"
SEND_TEXT_PATH = "/v3.0/oa/message/cs"
MAX_TEXT_LENGTH = 2000
UPLOAD_IMAGE_PATH = "/v2.0/oa/upload/image"
MAX_IMAGE_BYTES = 1_000_000
# Both message APIs return at most this many messages per request.
MAX_PAGE_SIZE = 10
TIMEOUT = httpx.Timeout(10.0)
READ_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0
SERVER_ERROR_STATUS = 500

# httpx logs request URLs at INFO and headers at DEBUG.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ZaloClient:
    def __init__(
        self,
        access_token: str,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not access_token.strip():
            raise ValueError("access_token is empty")
        self._token = access_token.strip()
        self._http = http or httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)
        self._sleep = sleep

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def send_text(self, user_id: str, text: str) -> SendResult:
        """Send a consultation text message to one user. Never retried."""
        if not text.strip() or len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"text must be 1-{MAX_TEXT_LENGTH} characters")
        return self._send_message(user_id, {"text": text})

    def upload_image(self, data: bytes, file_name: str) -> str:
        """Upload a JPG or PNG of at most 1 MB and return its attachment id.

        Not retried: each upload counts against the OA's monthly upload quota.
        """
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("image must be 1 byte to 1 MB")
        media_type = "image/png" if file_name.lower().endswith(".png") else "image/jpeg"
        try:
            response = self._http.post(
                UPLOAD_IMAGE_PATH,
                headers={"access_token": self._token},
                files={"file": (file_name, data, media_type)},
            )
        except httpx.TransportError as error:
            raise ZaloUnavailableError(None, "network error") from error
        if response.status_code >= SERVER_ERROR_STATUS:
            raise ZaloUnavailableError(None, f"HTTP {response.status_code}")
        attachment_id = _object(_checked(response).get("data")).get("attachment_id")
        if not attachment_id:
            raise ZaloError(None, "upload answered without an attachment id")
        return str(attachment_id)

    def send_image(self, user_id: str, attachment_id: str, caption: str) -> SendResult:
        """Send an uploaded image with a caption to one user. Never retried."""
        if len(caption) > MAX_TEXT_LENGTH:
            raise ValueError(f"caption must be at most {MAX_TEXT_LENGTH} characters")
        return self._send_message(
            user_id,
            {
                "text": caption,
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "media",
                        "elements": [
                            {"media_type": "image", "attachment_id": attachment_id}
                        ],
                    },
                },
            },
        )

    def _send_message(self, user_id: str, message: dict[str, Any]) -> SendResult:
        if not user_id.isdigit():
            raise ValueError("user_id must contain only digits")
        try:
            response = self._http.post(
                SEND_TEXT_PATH,
                headers={"access_token": self._token},
                json={"recipient": {"user_id": user_id}, "message": message},
            )
        except httpx.ConnectError:
            # The connection never opened, so Zalo did not receive the request.
            return SendResult(SendStatus.FAILED)
        except httpx.TransportError:
            # The request may have reached Zalo; sending again risks a duplicate.
            return SendResult(SendStatus.UNKNOWN)
        if response.status_code >= SERVER_ERROR_STATUS:
            return SendResult(SendStatus.UNKNOWN)
        try:
            body = response.json()
        except ValueError:
            return SendResult(SendStatus.UNKNOWN)
        if not isinstance(body, dict):
            return SendResult(SendStatus.UNKNOWN)
        try:
            code = int(body.get("error", -1))
        except TypeError, ValueError:
            return SendResult(SendStatus.UNKNOWN)
        if code != 0:
            return SendResult(
                SendStatus.FAILED,
                error_code=code,
                error_message=str(body.get("message", "")),
            )
        data = _object(body.get("data"))
        quota = _object(data.get("quota"))
        message_id = data.get("message_id")
        return SendResult(
            SendStatus.SENT,
            message_id=str(message_id) if message_id is not None else None,
            quota_type=str(quota["quota_type"]) if quota.get("quota_type") else None,
        )

    def recent_messages(
        self, offset: int, count: int = MAX_PAGE_SIZE
    ) -> list[ZaloMessage]:
        """The latest messages across all conversations, newest first."""
        return self._messages(RECENT_CHATS_PATH, {"offset": offset, "count": count})

    def conversation(
        self, user_id: str, offset: int, count: int = MAX_PAGE_SIZE
    ) -> list[ZaloMessage]:
        """Messages between the OA and one user, newest first."""
        if not user_id.isdigit():
            raise ValueError("user_id must contain only digits")
        # The API documents user_id as a number, and Python ints keep every digit.
        data = {"user_id": int(user_id), "offset": offset, "count": count}
        return self._messages(CONVERSATION_PATH, data)

    def _messages(self, path: str, data: Mapping[str, Any]) -> list[ZaloMessage]:
        if not 1 <= data["count"] <= MAX_PAGE_SIZE:
            raise ValueError(f"count must be 1-{MAX_PAGE_SIZE}")
        items = self._get(path, data).get("data") or []
        if not isinstance(items, list):
            raise ZaloError(None, "unexpected response shape")
        try:
            return [ZaloMessage.model_validate(item) for item in items]
        except ValidationError as error:
            raise ZaloError(None, "unexpected message shape") from error

    def _get(self, path: str, data: Mapping[str, Any]) -> dict[str, Any]:
        params = {"data": json.dumps(data, separators=(",", ":"))}
        headers = {"access_token": self._token}
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                response = self._http.get(path, params=params, headers=headers)
            except httpx.TransportError:
                failure = "network error"
            else:
                if response.status_code < SERVER_ERROR_STATUS:
                    return _checked(response)
                failure = f"HTTP {response.status_code}"
            if attempt < READ_ATTEMPTS:
                self._sleep(RETRY_DELAY_SECONDS * 2 ** (attempt - 1))
        raise ZaloUnavailableError(None, failure)


def refresh_oa_token(
    app_id: str,
    secret_key: str,
    refresh_token: str,
    http: httpx.Client | None = None,
) -> TokenPair:
    """Trade a refresh token for a new access and refresh token pair.

    The refresh token works once: whatever happens, the caller must store the new pair before
    doing anything else. Not retried here, because a request that reached Zalo has already
    spent the token; a second try would only be refused.
    """
    if not (app_id.strip() and secret_key.strip() and refresh_token.strip()):
        raise ValueError("app_id, secret_key and refresh_token are required")
    client = http or httpx.Client(timeout=TIMEOUT)
    try:
        response = client.post(
            OAUTH_TOKEN_URL,
            headers={"secret_key": secret_key.strip()},
            data={
                "refresh_token": refresh_token.strip(),
                "app_id": app_id.strip(),
                "grant_type": "refresh_token",
            },
        )
    except httpx.TransportError as error:
        raise ZaloUnavailableError(None, "network error") from error
    finally:
        if http is None:
            client.close()
    if response.status_code >= SERVER_ERROR_STATUS:
        raise ZaloUnavailableError(None, f"HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as error:
        raise ZaloRefreshRejectedError(
            None, f"HTTP {response.status_code}, not JSON"
        ) from error
    body = _object(body)
    try:
        return TokenPair.model_validate(body)
    except ValidationError:
        pass
    try:
        code: int | None = int(body["error"])
    except KeyError, TypeError, ValueError:
        code = None
    # Zalo repeats the same text in several fields; show each wording once.
    detail = "; ".join(
        dict.fromkeys(
            str(body[field]).strip() for field in OAUTH_ERROR_FIELDS if body.get(field)
        )
    )
    raise ZaloRefreshRejectedError(code, detail or "answer without new tokens")


def _checked(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as error:
        raise ZaloError(None, f"HTTP {response.status_code}, not JSON") from error
    if not isinstance(body, dict):
        raise ZaloError(None, "unexpected response shape")
    try:
        code = int(body.get("error", -1))
    except TypeError, ValueError:
        code = -1
    if code != 0:
        raise error_for(code, str(body.get("message", "")))
    return body


def _object(value: Any) -> dict[str, Any]:
    """A nested JSON object, or an empty one when Zalo left it out or changed its shape."""
    return value if isinstance(value, dict) else {}
