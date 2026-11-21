"""Zalo error codes turned into exceptions the caller can act on.

Zalo often answers HTTP 200 with a non-zero `error` field, so the code decides, not the status.
Messages never include the request: its header carries the access token.
"""

from enum import IntEnum


class ZaloErrorCode(IntEnum):
    RATE_LIMITED = -32
    INVALID_TOKEN = -216
    EXPIRED_TOKEN = -220
    APP_NOT_FOUND = -209
    APP_NOT_ACTIVE = -212
    APP_NO_PERMISSION = -219
    OA_NOT_ACTIVE = -221
    OA_NOT_APPROVED = -223
    OA_PLAN_REQUIRED = -224
    OA_FEATURE_NOT_ALLOWED = -235


AUTH_CODES = frozenset({ZaloErrorCode.INVALID_TOKEN, ZaloErrorCode.EXPIRED_TOKEN})
CONFIGURATION_CODES = frozenset(
    {
        ZaloErrorCode.APP_NOT_FOUND,
        ZaloErrorCode.APP_NOT_ACTIVE,
        ZaloErrorCode.APP_NO_PERMISSION,
        ZaloErrorCode.OA_NOT_ACTIVE,
        ZaloErrorCode.OA_NOT_APPROVED,
        ZaloErrorCode.OA_PLAN_REQUIRED,
        ZaloErrorCode.OA_FEATURE_NOT_ALLOWED,
    }
)


class ZaloError(Exception):
    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"Zalo error {code}: {message}")
        self.code = code
        self.message = message


class ZaloAuthError(ZaloError):
    """The access token is invalid or expired."""


class ZaloRateLimitError(ZaloError):
    """Too many calls this minute; wait for the next one."""


class ZaloConfigurationError(ZaloError):
    """The app or OA lacks a permission or service plan; retrying will not help."""


class ZaloRequestError(ZaloError):
    """Zalo refused this request for another reason."""


class ZaloUnavailableError(ZaloError):
    """Zalo could not be reached or failed on its side, even after retrying."""


class ZaloOAuthErrorCode(IntEnum):
    """Codes of oauth.zaloapp.com, which the documentation does not list; seen in practice."""

    # Seen 2026-11-19 with a wrong secret_key header. The tokens are kept rather than marked
    # unusable, but the refresh token may still have been spent: after this error the same
    # pair was later refused with -14014 (2026-11-21).
    INVALID_SECRET_KEY = -14004


class ZaloRefreshRejectedError(ZaloError):
    """Zalo answered a token refresh without new tokens: the refresh token was used, expired
    or revoked, or the app id or secret key is wrong. Retrying with it cannot help."""


def error_for(code: int, message: str) -> ZaloError:
    if code in AUTH_CODES:
        return ZaloAuthError(code, message)
    if code == ZaloErrorCode.RATE_LIMITED:
        return ZaloRateLimitError(code, message)
    if code in CONFIGURATION_CODES:
        return ZaloConfigurationError(code, message)
    return ZaloRequestError(code, message)
