"""Test page: read and send OA messages, and paste the OA tokens the app keeps refreshed.

Message text comes from outside the company and is shown only in tables, never as markdown
or HTML.
"""

from typing import Any

import streamlit as st

from gui_san_luong.services.zalo_messages import (
    Conversation,
    MessageArchive,
)
from gui_san_luong.ui.zalo_send_section import render_send_section
from gui_san_luong.ui.zalo_state import (
    TOKEN_KEY,
    TOKEN_MISSING,
    access_token,
    epoch_ms,
    forget_archive,
    format_vietnam_time,
    load_archive,
    loaded_archive,
    pasted_token,
    vietnam_datetime,
)
from gui_san_luong.ui.zalo_token_section import render_token_section
from gui_san_luong.zalo.schemas import MessageSource, ZaloMessage

EXTRA_IDS_KEY = "zalo_test_extra_user_ids"
ALL_USERS = "__all__"
DIRECTION_LABELS = {
    MessageSource.FROM_USER: "Người dùng → OA",
    MessageSource.FROM_OA: "OA → Người dùng",
}


def _parse_user_ids(text: str) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    invalid: list[str] = []
    for part in text.replace(",", " ").split():
        (ids if part.isdigit() else invalid).append(part)
    return list(dict.fromkeys(ids)), invalid


def _attachment(message: ZaloMessage) -> str:
    if message.url or message.thumb:
        return message.url or message.thumb or ""
    if message.links:
        return ", ".join(str(link) for link in message.links)
    return str(message.location) if message.location else ""


def _rows(conversations: list[Conversation]) -> list[dict[str, Any]]:
    """Newest first, so the latest message is at the top whichever users are shown."""
    pairs = sorted(
        (
            (message, conversation)
            for conversation in conversations
            for message in conversation.messages
        ),
        key=lambda pair: pair[0].time,
        reverse=True,
    )
    return [
        {
            "Thời gian": vietnam_datetime(message.time),
            "Người dùng": conversation.display_name or "",
            "Zalo user id": conversation.user_id,
            "Chiều": DIRECTION_LABELS[message.src],
            "Loại": message.type or "",
            "Nội dung": message.message or message.description or "",
            "Đính kèm": _attachment(message),
        }
        for message, conversation in pairs
    ]


def _show_archive(archive: MessageArchive) -> None:
    conversations = list(archive.conversations)
    from_users = sum(
        message.src is MessageSource.FROM_USER
        for conversation in conversations
        for message in conversation.messages
    )
    people, messages, incoming = st.columns(3)
    people.metric("Người dùng", f"{len(conversations):,}")
    messages.metric("Tin nhắn", f"{archive.message_count:,}")
    incoming.metric("Tin người dùng gửi", f"{from_users:,}")
    if not conversations:
        st.info("Không có tin nhắn nào.", icon=":material/info:")
        return
    if truncated := [c for c in conversations if c.truncated]:
        st.warning(
            f"{len(truncated):,} hội thoại quá dài nên chỉ lấy những tin mới nhất.",
            icon=":material/warning:",
        )

    by_id = {conversation.user_id: conversation for conversation in conversations}
    choice = st.selectbox(
        "Xem hội thoại",
        [ALL_USERS, *by_id],
        format_func=lambda user_id: (
            "Tất cả người dùng"
            if user_id == ALL_USERS
            else f"{by_id[user_id].display_name or 'Không rõ tên'} · {user_id} · "
            f"{len(by_id[user_id].messages):,} tin"
        ),
    )
    shown = conversations if choice == ALL_USERS else [by_id[choice]]
    st.dataframe(
        _rows(shown),
        hide_index=True,
        width="stretch",
        column_config={
            "Thời gian": st.column_config.DatetimeColumn(
                "Thời gian", format="HH:mm:ss DD/MM/YYYY"
            ),
            "Zalo user id": st.column_config.TextColumn("Zalo user id"),
            "Nội dung": st.column_config.TextColumn("Nội dung", width="large"),
        },
    )


def render_zalo_messages() -> None:
    st.subheader("Tin nhắn Zalo (thử nghiệm)", anchor=False)
    st.caption("Đọc tin nhắn giữa OA và người dùng, và gửi tin thử, qua Zalo OA API.")
    with st.container(border=True):
        render_token_section()
        st.caption(
            "Ứng dụng cần có **Quyền quản lý tin nhắn người dùng** để đọc tin và "
            "**Quyền gửi tin và thông báo qua OA** để gửi tin."
        )
    with st.expander("Dùng tạm một access token khác trong phiên này"):
        st.text_input(
            "OA access token",
            type="password",
            key=TOKEN_KEY,
            placeholder="Dán access token của OA",
            help="Chỉ dùng trong phiên trình duyệt này, không lưu và không tự làm mới.",
        )
        if st.button("Bỏ token dùng tạm", icon=":material/key_off:"):
            st.session_state.pop(TOKEN_KEY, None)
            st.rerun()
    token = access_token()
    if pasted_token():
        st.caption(":material/key: Đang dùng token dán tạm (chỉ trong phiên này).")
    elif not token:
        st.warning(TOKEN_MISSING, icon=":material/key_off:")
    ids_text = st.text_input(
        "Zalo user id cần lấy thêm (không bắt buộc)",
        key=EXTRA_IDS_KEY,
        help=(
            "Zalo chỉ cho biết những người nhắn gần đây. Người nhắn đã lâu có thể không "
            "có trong danh sách: nhập user id của họ, cách nhau bằng dấu phẩy."
        ),
    )
    extra_ids, invalid = _parse_user_ids(ids_text)
    if invalid:
        st.warning(
            "Bỏ qua vì user id chỉ gồm chữ số: " + ", ".join(invalid),
            icon=":material/warning:",
        )

    with st.container(horizontal=True):
        load_all = st.button(
            "Tải tất cả tin nhắn",
            type="primary",
            icon=":material/download:",
            disabled=not token.strip(),
        )
        load_extra = st.button(
            "Chỉ tải các user id đã nhập",
            icon=":material/person_search:",
            disabled=not (token.strip() and extra_ids),
        )
        if st.button(
            "Xóa tin nhắn đã lưu",
            icon=":material/delete:",
            help=(
                "Tin đã tải được lưu trên máy để không mất khi tải lại trang. Xóa đi thì "
                "phải tải lại từ Zalo mới biết ai đã tương tác."
            ),
        ):
            forget_archive()
    if load_all or load_extra:
        load_archive(token, extra_ids, only_extra=load_extra)

    archive = loaded_archive()
    if archive is not None:
        st.caption(
            f":material/save: Đang dùng tin nhắn đã lưu, tải lần gần nhất lúc "
            f"{format_vietnam_time(epoch_ms(archive.fetched_at))}."
        )
        _show_archive(archive)

    st.divider()
    render_send_section(token, archive)
