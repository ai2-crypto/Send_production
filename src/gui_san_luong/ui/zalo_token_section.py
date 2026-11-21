"""Paste the OA tokens once, see when they expire, and get warned when a refresh fails."""

from datetime import UTC, datetime, timedelta

import streamlit as st

from gui_san_luong.config import database_path
from gui_san_luong.services.zalo_token import (
    TokenSettings,
    TokenSetupError,
    TokenStatus,
    TokenStorageError,
    forget_tokens,
    refresh_now,
    save_pasted_tokens,
    token_status,
)
from gui_san_luong.ui.zalo_state import VIETNAM_TIME
from gui_san_luong.zalo.errors import ZaloError

EXPLORER_URL = "https://developers.zalo.me/tools/explorer"
ACCESS_INPUT_KEY = "zalo_paste_access_token"
REFRESH_INPUT_KEY = "zalo_paste_refresh_token"
NOTICE_KEY = "zalo_token_notice"
# Warn this long before the refresh token runs out, so there is time to paste new tokens.
REFRESH_EXPIRY_WARNING = timedelta(days=14)
KEY_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; '
    'print(Fernet.generate_key().decode())"'
)


def _when(moment: datetime) -> str:
    return f"{moment.astimezone(VIETNAM_TIME):%H:%M %d/%m/%Y}"


def _now() -> datetime:
    return datetime.now(UTC)


def _status() -> TokenStatus | None:
    try:
        return token_status(database_path())
    except TokenStorageError as error:
        st.error(str(error), icon=":material/error:")
        return None


def render_token_warning() -> None:
    """A banner at the top of the page whenever the automatic refresh needs attention."""
    try:
        status = token_status(database_path())
    except TokenStorageError:
        return
    if status is None:
        return
    if status.needs_reauthorization:
        st.error(
            f"Không tự làm mới được access token Zalo. {status.last_error or ''} Access "
            "token đang dùng vẫn chạy đến khi hết hạn (tối đa 25 giờ kể từ lúc lấy ở API "
            "Explorer), sau đó sẽ không gửi được. Vào tab “Tin nhắn Zalo (thử)” để dán "
            "token mới.",
            icon=":material/key_off:",
        )
    elif status.last_error:
        st.warning(status.last_error, icon=":material/sync_problem:")
    elif status.refresh_expires_at - _now() < REFRESH_EXPIRY_WARNING:
        st.warning(
            "Refresh token Zalo sắp hết hạn (khoảng "
            f"{_when(status.refresh_expires_at)}). Hãy lấy token mới ở API Explorer rồi "
            "dán lại ở tab “Tin nhắn Zalo (thử)”.",
            icon=":material/schedule:",
        )


def _show_status(status: TokenStatus | None) -> None:
    if status is None:
        st.info(
            "Chưa lưu token nào. Dán access token và refresh token bên dưới.",
            icon=":material/key:",
        )
        return
    access_line = (
        f"- Access token hết hạn lúc **{_when(status.access_expires_at)}**; trang tự "
        "làm mới khi còn dưới 1 giờ."
        if status.expiry_known
        else "- Access token vừa dán: chưa biết hạn (thường còn tối đa 25 giờ kể từ lúc "
        "lấy ở API Explorer). Trang biết hạn sau lần làm mới thành công đầu tiên."
    )
    lines = [
        access_line,
        (
            "- Refresh token dùng được đến khoảng "
            f"**{_when(status.refresh_expires_at)}**."
        ),
        f"- Cập nhật lần cuối lúc {_when(status.updated_at)}.",
    ]
    st.markdown("\n".join(lines))
    if status.needs_reauthorization:
        st.error(status.last_error or "Cần dán token mới.", icon=":material/key_off:")
    elif status.last_error:
        st.warning(status.last_error, icon=":material/sync_problem:")


def _refresh(settings: TokenSettings) -> None:
    try:
        refresh_now(database_path(), settings, _now)
    except ZaloError, TokenSetupError, TokenStorageError:
        # The failure is stored with the tokens and shown in the status above.
        st.session_state[NOTICE_KEY] = None
    else:
        st.session_state[NOTICE_KEY] = "Đã làm mới access token."
    st.rerun()


def _save_pasted(settings: TokenSettings, access: str, refresh: str) -> None:
    try:
        save_pasted_tokens(database_path(), settings, access, refresh, _now())
    except (TokenSetupError, TokenStorageError) as error:
        st.error(str(error), icon=":material/error:")
        return
    st.session_state.pop(ACCESS_INPUT_KEY, None)
    st.session_state.pop(REFRESH_INPUT_KEY, None)
    # Refresh at once: it proves the app id and secret key, and gives the exact expiry.
    _refresh(settings)


def render_token_section() -> None:
    settings = TokenSettings.from_environment()
    st.markdown("**Access token Zalo (tự làm mới)**")
    if notice := st.session_state.pop(NOTICE_KEY, None):
        st.success(notice, icon=":material/check_circle:")
    if missing := settings.missing():
        st.warning(
            "Thiếu "
            + ", ".join(f"`{name}`" for name in missing)
            + " trong file `.env`, "
            "nên chưa tự làm mới được token. App ID và Secret key lấy ở trang quản lý "
            "ứng dụng trên Zalo for Developers. Tạo `TOKEN_ENCRYPTION_KEY` bằng lệnh: "
            f"`{KEY_COMMAND}`",
            icon=":material/settings:",
        )
    status = _status()
    _show_status(status)

    with st.form("zalo_token_form", clear_on_submit=False, border=True):
        st.markdown(
            f"Mở [API Explorer]({EXPLORER_URL}), chọn **OA Access Token**, ứng dụng và OA, "
            "bấm **Lấy Access Token**, rồi dán cả hai token vào đây. Chỉ cần làm một lần: "
            "sau đó trang tự làm mới. Không dùng token này ở nơi khác (Postman, máy khác), "
            "vì mỗi refresh token chỉ dùng được một lần."
        )
        access = st.text_input("Access token", type="password", key=ACCESS_INPUT_KEY)
        refresh = st.text_input("Refresh token", type="password", key=REFRESH_INPUT_KEY)
        if missing:
            st.caption(
                ":material/lock: Chưa lưu được: điền "
                + ", ".join(f"`{name}`" for name in missing)
                + " vào `.env` (xem khung cảnh báo phía trên). Trang đọc lại `.env` mỗi "
                "lần dùng, nên điền xong chỉ cần tải lại trang."
            )
        if st.form_submit_button(
            "Lưu và làm mới ngay",
            type="primary",
            icon=":material/save:",
            disabled=bool(missing),
        ):
            _save_pasted(settings, access, refresh)

    with st.container(horizontal=True):
        if st.button(
            "Làm mới ngay",
            icon=":material/refresh:",
            disabled=status is None
            or status.needs_reauthorization
            or bool(settings.missing()),
            help="Thường không cần: trang tự làm mới khi access token sắp hết hạn.",
        ):
            _refresh(settings)
        if st.button(
            "Xóa token đã lưu",
            icon=":material/delete:",
            disabled=status is None,
        ):
            try:
                forget_tokens(database_path(), _now())
            except TokenStorageError as error:
                st.error(str(error), icon=":material/error:")
            else:
                st.rerun()
