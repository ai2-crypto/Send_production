"""Notice bar: who texted the OA in the last 48 hours and can be messaged for free until when."""

from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

from gui_san_luong.services.zalo_codes import free_window_users
from gui_san_luong.services.zalo_links import ZaloLinkStorageError, load_links
from gui_san_luong.ui.zalo_state import (
    epoch_ms,
    format_vietnam_time,
    loaded_archive,
)

HOUR_MS = 60 * 60 * 1000


def _time_left(until_ms: int, now_ms: int) -> str:
    hours, rest = divmod(max(until_ms - now_ms, 0), HOUR_MS)
    return f"{hours} giờ {rest // 60_000:02d} phút"


def render_free_window_notice(db_path: Path) -> None:
    archive = loaded_archive()
    if archive is None:
        return
    now = datetime.now(UTC)
    users = free_window_users(archive, now)
    loaded = format_vietnam_time(epoch_ms(archive.fetched_at))
    if not users:
        st.info(
            f"Không ai tương tác với OA trong 48 giờ qua (theo tin tải lúc {loaded}). "
            "Gửi tin lúc này có thể bị tính phí hoặc bị từ chối.",
            icon=":material/schedule:",
        )
        return
    try:
        codes = {link.zalo_user_id: link.employee_code for link in load_links(db_path)}
    except ZaloLinkStorageError:
        codes = {}
    now_ms = epoch_ms(now)
    linked = sum(user.user_id in codes for user in users)
    with st.expander(
        f":material/mark_chat_read: {len(users):,} người đã tương tác trong 48 giờ, "
        f"gửi tin miễn phí ({linked:,} người đã nối mã nhân viên)",
        expanded=False,
    ):
        st.caption(
            f"Theo tin nhắn tải lúc {loaded}. Người sắp hết hạn ở trên cùng; tải lại tin "
            "nhắn để cập nhật."
        )
        st.dataframe(
            [
                {
                    "Tên Zalo": user.display_name or "",
                    "Mã NV": codes.get(user.user_id, ""),
                    "Tương tác lúc": format_vietnam_time(user.last_interaction_ms),
                    "Miễn phí đến": format_vietnam_time(user.free_until_ms),
                    "Còn lại": _time_left(user.free_until_ms, now_ms),
                    "Zalo user id": user.user_id,
                }
                for user in users
            ],
            hide_index=True,
            width="stretch",
        )
