"""Tab “Tin nhắn Zalo”: tin nhắn OA, người trong 48 giờ miễn phí, gửi tin thử và token."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from gui_san_luong.api.context import API_ACTOR, ApiContext, get_context
from gui_san_luong.api.errors import ApiError, ErrorCode
from gui_san_luong.api.schemas import (
    ActiveUserOut,
    ArchiveOut,
    ConversationOut,
    ForgetOut,
    FreeWindowOut,
    MessageOut,
    RefreshOut,
    ScanIn,
    TestMessageIn,
    TestMessageOut,
    TokenIn,
    TokenStatusOut,
    message_direction,
)
from gui_san_luong.api.views import from_epoch_ms
from gui_san_luong.services.message_store import (
    forget_stored_messages,
    load_stored_archive,
    store_archive,
)
from gui_san_luong.services.zalo_codes import free_window_users
from gui_san_luong.services.zalo_links import load_links
from gui_san_luong.services.zalo_messages import (
    MessageArchive,
    fetch_all_messages,
    fetch_conversation,
)
from gui_san_luong.services.zalo_send import send_test_message
from gui_san_luong.services.zalo_texts import send_failure_text
from gui_san_luong.services.zalo_token import (
    TokenSettings,
    forget_tokens,
    refresh_now,
    save_pasted_tokens,
    token_status,
)
from gui_san_luong.zalo.errors import ZaloError
from gui_san_luong.zalo.schemas import MessageSource, SendStatus, ZaloMessage

router = APIRouter(prefix="/zalo", tags=["Zalo"])
Context = Annotated[ApiContext, Depends(get_context)]


def _token(context: ApiContext) -> str:
    token = context.access_token(context.db_path)
    if not token:
        raise ApiError(
            409,
            ErrorCode.TOKEN_MISSING,
            "Chưa có access token Zalo: hãy lưu token (PUT /zalo/token).",
        )
    return token


def _attachment(message: ZaloMessage) -> str | None:
    if message.url or message.thumb:
        return message.url or message.thumb
    if message.links:
        return ", ".join(str(link) for link in message.links)
    return str(message.location) if message.location else None


def _archive_out(archive: MessageArchive) -> ArchiveOut:
    return ArchiveOut(
        fetched_at=archive.fetched_at,
        user_count=len(archive.conversations),
        message_count=archive.message_count,
        from_user_count=sum(
            message.src is MessageSource.FROM_USER
            for conversation in archive.conversations
            for message in conversation.messages
        ),
        conversations=[
            ConversationOut(
                user_id=conversation.user_id,
                display_name=conversation.display_name,
                truncated=conversation.truncated,
                messages=[
                    MessageOut(
                        message_id=message.message_id,
                        direction=message_direction(message.src),
                        sent_at=from_epoch_ms(message.time),
                        type=message.type,
                        # Text from outside the company: a frontend must show it as text.
                        text=message.message or message.description,
                        attachment=_attachment(message),
                    )
                    for message in conversation.messages
                ],
            )
            for conversation in archive.conversations
        ],
    )


@router.get(
    "/messages",
    summary="Tin nhắn OA đã quét (lưu trên máy chủ)",
    description="Trả `null` khi chưa quét lần nào. Nội dung tin là dữ liệu từ bên ngoài: "
    "frontend phải hiển thị dạng chữ thuần, không render HTML/markdown.",
)
def get_messages(context: Context) -> ArchiveOut | None:
    archive = load_stored_archive(context.db_path)
    return _archive_out(archive) if archive else None


@router.post("/messages/scan", summary="Quét tin nhắn mới từ Zalo")
def scan_messages(context: Context, body: ScanIn) -> ArchiveOut:
    invalid = [user_id for user_id in body.extra_user_ids if not user_id.isdigit()]
    if invalid:
        raise ApiError(
            422,
            ErrorCode.INVALID_REQUEST,
            "User id chỉ gồm chữ số: " + ", ".join(invalid),
        )
    if body.only_extra and not body.extra_user_ids:
        raise ApiError(422, ErrorCode.INVALID_REQUEST, "Hãy nêu user id cần tải.")
    kept = load_stored_archive(context.db_path)
    with context.zalo_client(_token(context)) as client:
        if body.only_extra:
            archive = MessageArchive(
                tuple(
                    fetch_conversation(client, user_id)
                    for user_id in dict.fromkeys(body.extra_user_ids)
                )
            )
        else:
            # Read only what is newer than the kept messages, and look again at everyone
            # who ever wrote: Zalo's recent list can miss quiet people.
            known_ids = frozenset(
                message.message_id
                for conversation in (kept.conversations if kept else ())
                for message in conversation.messages
            )
            kept_users = [c.user_id for c in kept.conversations] if kept else []
            archive = fetch_all_messages(
                client, [*body.extra_user_ids, *kept_users], None, known_ids
            )
    return _archive_out(store_archive(context.db_path, archive))


@router.delete("/messages", summary="Xóa tin nhắn đã lưu trên máy chủ")
def forget_messages(context: Context) -> ForgetOut:
    return ForgetOut(deleted=forget_stored_messages(context.db_path))


@router.get(
    "/free-window",
    summary="Người đã nhắn OA trong 48 giờ qua (gửi tin miễn phí)",
)
def free_window(context: Context) -> FreeWindowOut:
    archive = load_stored_archive(context.db_path)
    if archive is None:
        return FreeWindowOut(messages_loaded_at=None, users=[])
    codes = {
        link.zalo_user_id: link.employee_code for link in load_links(context.db_path)
    }
    return FreeWindowOut(
        messages_loaded_at=archive.fetched_at,
        users=[
            ActiveUserOut(
                user_id=user.user_id,
                display_name=user.display_name,
                employee_code=codes.get(user.user_id),
                last_interaction_at=from_epoch_ms(user.last_interaction_ms),
                free_until=from_epoch_ms(user.free_until_ms),
            )
            for user in free_window_users(archive, context.now())
        ],
    )


@router.post(
    "/test-message",
    summary="Gửi một tin thử dạng chữ cho một người (không thu hồi được)",
)
def test_message(context: Context, body: TestMessageIn) -> TestMessageOut:
    if not body.confirm:
        raise ApiError(
            409,
            ErrorCode.CONFIRMATION_REQUIRED,
            "Tin đã gửi không thu hồi được: hãy gửi kèm confirm = true.",
        )
    with context.zalo_client(_token(context)) as client:
        sent = send_test_message(
            context.db_path, client, body.user_id, body.text, API_ACTOR
        )
    result = sent.result
    detail = (
        "Đã gửi."
        if result.status is SendStatus.SENT
        else "Không rõ Zalo đã nhận chưa: đừng gửi lại ngay, xem hội thoại trước."
        if result.status is SendStatus.UNKNOWN
        else send_failure_text(result.error_code, result.error_message)
    )
    return TestMessageOut(
        status=result.status,
        message_id=result.message_id,
        error_code=result.error_code,
        detail=detail,
        audit_failed=sent.audit_failed,
    )


# ---------------------------------------------------------------- token


def _status_out(context: ApiContext) -> TokenStatusOut:
    settings = TokenSettings.from_environment()
    status_ = token_status(context.db_path)
    return TokenStatusOut(
        stored=status_ is not None,
        missing_settings=settings.missing(),
        access_expires_at=status_.access_expires_at if status_ else None,
        access_expiry_known=status_.expiry_known if status_ else False,
        refresh_expires_at=status_.refresh_expires_at if status_ else None,
        updated_at=status_.updated_at if status_ else None,
        last_error=status_.last_error if status_ else None,
        needs_reauthorization=status_.needs_reauthorization if status_ else False,
        usable=bool(context.access_token(context.db_path)),
    )


def _refresh(context: ApiContext) -> RefreshOut:
    try:
        refresh_now(context.db_path, TokenSettings.from_environment(), context.now)
    except ZaloError:
        # The failure is stored with the tokens and returned in the status.
        return RefreshOut(refreshed=False, status=_status_out(context))
    return RefreshOut(refreshed=True, status=_status_out(context))


@router.get("/token", summary="Tình trạng access token Zalo (không bao giờ trả token)")
def get_token(context: Context) -> TokenStatusOut:
    return _status_out(context)


@router.put(
    "/token",
    summary="Lưu access token và refresh token lấy từ API Explorer, rồi làm mới ngay",
    description="Không dùng refresh token này ở nơi khác: mỗi refresh token chỉ dùng được "
    "một lần. Cần ZALO_APP_ID, ZALO_SECRET_KEY, TOKEN_ENCRYPTION_KEY trong .env.",
)
def put_token(context: Context, body: TokenIn) -> RefreshOut:
    settings = TokenSettings.from_environment()
    if missing := settings.missing():
        raise ApiError(
            409,
            ErrorCode.TOKEN_SETUP,
            "Thiếu " + ", ".join(missing) + " trong .env.",
            missing,
        )
    save_pasted_tokens(
        context.db_path, settings, body.access_token, body.refresh_token, context.now()
    )
    return _refresh(context)


@router.post("/token/refresh", summary="Làm mới access token ngay")
def refresh_token(context: Context) -> RefreshOut:
    return _refresh(context)


@router.delete(
    "/token",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Xóa token đã lưu",
)
def delete_token(context: Context) -> Response:
    forget_tokens(context.db_path, context.now())
    return Response(status_code=status.HTTP_204_NO_CONTENT)
