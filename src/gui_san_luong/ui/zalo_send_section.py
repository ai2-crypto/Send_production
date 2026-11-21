"""Test page section: send one text message from the OA to one user.

A sent message cannot be recalled, so the operator ticks a confirmation for each message, the
same text cannot go twice in a row to the same person, and a message whose outcome is unknown
is never offered for resending as if it had failed.
"""

import hashlib
from datetime import UTC, datetime

import streamlit as st

from gui_san_luong.config import database_path
from gui_san_luong.domain.send_window import Eligibility
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.services.zalo_send import eligibility, send_test_message
from gui_san_luong.ui.zalo_state import TOKEN_MISSING, send_failure_text
from gui_san_luong.zalo.client import MAX_TEXT_LENGTH, ZaloClient
from gui_san_luong.zalo.schemas import SendStatus

# There is no login yet, so a send cannot be attributed to a person.
LOCAL_ACTOR = "local"
RECIPIENT_KEY = "zalo_send_recipient"
OTHER_ID_KEY = "zalo_send_other_id"
TEXT_KEY = "zalo_send_text"
CONFIRM_KEY = "zalo_send_confirm"
NOTICE_KEY = "zalo_send_notice"
LAST_SENT_KEY = "zalo_send_last"
OTHER_RECIPIENT = "__other__"


def _show_notice() -> None:
    notice: tuple[SendStatus, str] | None = st.session_state.get(NOTICE_KEY)
    if notice is None:
        return
    status, text = notice
    if status is SendStatus.SENT:
        st.success(text, icon=":material/check_circle:")
    elif status is SendStatus.UNKNOWN:
        st.warning(text, icon=":material/help:")
    else:
        st.error(text, icon=":material/error:")


def _choose_recipient(conversations: dict[str, Conversation]) -> str | None:
    choice = st.selectbox(
        "Người nhận",
        [*conversations, OTHER_RECIPIENT],
        key=RECIPIENT_KEY,
        format_func=lambda user_id: (
            "Nhập Zalo user id khác…"
            if user_id == OTHER_RECIPIENT
            else f"{conversations[user_id].display_name or 'Không rõ tên'} · {user_id}"
        ),
    )
    if choice != OTHER_RECIPIENT:
        return choice
    typed = st.text_input("Zalo user id người nhận", key=OTHER_ID_KEY).strip()
    if typed and not typed.isdigit():
        st.warning("Zalo user id chỉ gồm chữ số.", icon=":material/warning:")
        return None
    return typed or None


def _show_eligibility(conversation: Conversation | None) -> bool:
    """Explain what Zalo will likely do; False when it is sure to refuse."""
    if conversation is None:
        st.info(
            "Chưa tải hội thoại của người này nên chưa biết lần tương tác cuối. Zalo sẽ "
            "từ chối nếu họ quá 7 ngày không tương tác với OA.",
            icon=":material/info:",
        )
        return True
    window = eligibility(conversation.messages, datetime.now(UTC))
    if window is Eligibility.FREE:
        st.success(
            "Người này tương tác trong 48 giờ qua: gửi miễn phí.",
            icon=":material/check:",
        )
        return True
    if window is Eligibility.PAID:
        st.warning(
            "Lần tương tác cuối đã quá 48 giờ (chưa quá 7 ngày): Zalo có thể tính phí tin này.",
            icon=":material/payments:",
        )
        return True
    st.error(
        "Người này chưa nhắn cho OA trong 7 ngày qua nên Zalo sẽ từ chối. Hãy nhờ họ nhắn "
        "một tin đến OA trước.",
        icon=":material/block:",
    )
    return False


def _signature(user_id: str, text: str) -> str:
    return hashlib.sha256(f"{user_id}\n{text}".encode()).hexdigest()


def _send(token: str, user_id: str, text: str) -> None:
    signature = _signature(user_id, text)
    if st.session_state.get(LAST_SENT_KEY) == signature:
        st.error(
            "Tin giống hệt vừa gửi cho người này. Sửa nội dung nếu thật sự muốn gửi lại.",
            icon=":material/content_copy:",
        )
        return
    with ZaloClient(token) as client, st.spinner("Đang gửi…"):
        sent = send_test_message(database_path(), client, user_id, text, LOCAL_ACTOR)
    result = sent.result
    if result.status is SendStatus.SENT:
        note = (
            f"Đã gửi tin đến {user_id} (message id {result.message_id or 'không rõ'})."
        )
    elif result.status is SendStatus.UNKNOWN:
        note = (
            "Không rõ Zalo đã nhận tin hay chưa (mất kết nối hoặc Zalo lỗi). Đừng gửi lại "
            "ngay: hãy bấm “Tải tất cả tin nhắn” để xem tin đã có trong hội thoại chưa."
        )
    else:
        note = send_failure_text(result.error_code, result.error_message)
    if sent.audit_failed:
        note += " Không ghi được nhật ký gửi vào cơ sở dữ liệu."
    if result.status is not SendStatus.FAILED:
        st.session_state[LAST_SENT_KEY] = signature
        # A clear box keeps a second click from sending the same message again.
        st.session_state.pop(TEXT_KEY, None)
    st.session_state.pop(CONFIRM_KEY, None)
    st.session_state[NOTICE_KEY] = (result.status, note)
    st.rerun()


def render_send_section(token: str, archive: MessageArchive | None) -> None:
    st.markdown("**:material/send: Gửi tin thử**")
    st.caption(
        "Gửi một tin Tư vấn dạng văn bản từ OA đến một người. Tin đã gửi không thu hồi được."
    )
    _show_notice()
    conversations = {c.user_id: c for c in archive.conversations} if archive else {}
    user_id = _choose_recipient(conversations)
    allowed = _show_eligibility(conversations.get(user_id or ""))
    text = st.text_area(
        "Nội dung tin",
        key=TEXT_KEY,
        max_chars=MAX_TEXT_LENGTH,
        placeholder="Nhập nội dung tin nhắn",
    )
    confirmed = st.checkbox(
        f"Tôi xác nhận gửi tin này đến {user_id or '…'}",
        key=CONFIRM_KEY,
        disabled=not (user_id and text.strip()),
    )
    ready = bool(token.strip() and user_id and text.strip() and confirmed and allowed)
    if st.button(
        "Gửi tin",
        type="primary",
        icon=":material/send:",
        disabled=not ready,
        help=None if token.strip() else TOKEN_MISSING,
    ):
        assert user_id is not None
        _send(token, user_id, text)
