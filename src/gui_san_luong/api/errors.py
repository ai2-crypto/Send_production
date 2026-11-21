"""Every error leaves as {"error": {"code", "message", "details"}}, with a Vietnamese message."""

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gui_san_luong.services.message_store import MessageStorageError
from gui_san_luong.services.output_changes import ChangeStorageError
from gui_san_luong.services.output_dispatch import DispatchStorageError
from gui_san_luong.services.zalo_links import InvalidLinksError, ZaloLinkStorageError
from gui_san_luong.services.zalo_texts import error_text
from gui_san_luong.services.zalo_token import TokenSetupError, TokenStorageError
from gui_san_luong.zalo.errors import ZaloAuthError, ZaloError, ZaloRateLimitError

# Each service raises its own when SQLite fails; all mean the same to a caller.
STORAGE_ERRORS: tuple[type[Exception], ...] = (
    ChangeStorageError,
    DispatchStorageError,
    MessageStorageError,
    TokenStorageError,
    ZaloLinkStorageError,
)


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    INVALID_REQUEST = "invalid_request"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_STALE = "confirmation_stale"
    TOKEN_MISSING = "token_missing"
    TOKEN_SETUP = "token_setup"
    ZALO_AUTH = "zalo_auth"
    ZALO_RATE_LIMITED = "zalo_rate_limited"
    ZALO_ERROR = "zalo_error"
    STORAGE = "storage"
    INVALID_LINKS = "invalid_links"
    IMAGE_NOT_AVAILABLE = "image_not_available"


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: ErrorCode,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def not_found(message: str) -> ApiError:
    return ApiError(404, ErrorCode.NOT_FOUND, message)


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(
            _body(error.code, error.message, error.details), status_code=error.status
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, error: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in item["loc"]),
                "problem": item["msg"],
            }
            for item in error.errors()
        ]
        return JSONResponse(
            _body(ErrorCode.INVALID_REQUEST, "Dữ liệu gửi lên không hợp lệ.", details),
            status_code=422,
        )

    @app.exception_handler(InvalidLinksError)
    async def _links(_: Request, error: InvalidLinksError) -> JSONResponse:
        return JSONResponse(
            _body(
                ErrorCode.INVALID_LINKS,
                "Chưa lưu gì vì có dòng cần sửa.",
                error.problems,
            ),
            status_code=422,
        )

    @app.exception_handler(TokenSetupError)
    async def _token_setup(_: Request, error: TokenSetupError) -> JSONResponse:
        return JSONResponse(_body(ErrorCode.TOKEN_SETUP, str(error)), status_code=409)

    @app.exception_handler(ZaloError)
    async def _zalo(_: Request, error: ZaloError) -> JSONResponse:
        if isinstance(error, ZaloAuthError):
            code, status = ErrorCode.ZALO_AUTH, 401
        elif isinstance(error, ZaloRateLimitError):
            code, status = ErrorCode.ZALO_RATE_LIMITED, 429
        else:
            code, status = ErrorCode.ZALO_ERROR, 502
        return JSONResponse(
            _body(code, error_text(error), {"zalo_code": error.code}),
            status_code=status,
        )

    for error_type in STORAGE_ERRORS:

        @app.exception_handler(error_type)
        async def _storage(_: Request, error: Exception) -> JSONResponse:
            return JSONResponse(_body(ErrorCode.STORAGE, str(error)), status_code=503)
