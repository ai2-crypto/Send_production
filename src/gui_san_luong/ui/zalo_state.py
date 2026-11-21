"""What the Zalo parts of the page share: the access token, the loaded messages, Vietnam time.

The token is normally the stored one, which the app refreshes before it expires (see
services/zalo_token.py). A token pasted on the messages tab overrides it for this browser
session only, and ZALO_OA_ACCESS_TOKEN in .env is the fallback when nothing is stored.
"""

from datetime import UTC, datetime, timedelta, timezone

import streamlit as st

from gui_san_luong.config import database_path
from gui_san_luong.services.message_store import (
    MessageStorageError,
    forget_stored_messages,
    load_stored_archive,
    store_archive,
)
from gui_san_luong.services.zalo_messages import (
    MessageArchive,
    fetch_all_messages,
    fetch_conversation,
)
from gui_san_luong.services.zalo_texts import error_text, send_failure_text
from gui_san_luong.services.zalo_token import resolve_access_token
from gui_san_luong.zalo.client import ZaloClient
from gui_san_luong.zalo.errors import (
    ZaloError,
)

TOKEN_KEY = "zalo_test_access_token"
ARCHIVE_KEY = "zalo_test_archive"
# Vietnam keeps UTC+7 all year, so a fixed offset needs no time zone database.
VIETNAM_TIME = timezone(timedelta(hours=7))
TOKEN_MISSING = "Chưa có OA access token: dán access token và refresh token ở tab “Tin nhắn Zalo (thử)”."


def vietnam_datetime(epoch_ms: int) -> datetime:
    """Naive Vietnam time, as Streamlit tables show a naive datetime unchanged."""
    moment = datetime.fromtimestamp(epoch_ms / 1000, UTC).astimezone(VIETNAM_TIME)
    return moment.replace(tzinfo=None)


def format_vietnam_time(epoch_ms: int) -> str:
    return f"{vietnam_datetime(epoch_ms):%H:%M %d/%m/%Y}"


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def pasted_token() -> str:
    token = st.session_state.get(TOKEN_KEY)
    return token.strip() if isinstance(token, str) else ""


def access_token() -> str:
    """The token for this session, in order of preference.

    A token pasted for this session only; then the stored token, refreshed by the app; then
    ZALO_OA_ACCESS_TOKEN from the environment or .env, kept for setups without stored tokens.
    """
    return pasted_token() or resolve_access_token(database_path())


def loaded_archive() -> MessageArchive | None:
    """The messages of this session, restored from the database after a page refresh."""
    if ARCHIVE_KEY not in st.session_state:
        try:
            st.session_state[ARCHIVE_KEY] = load_stored_archive(database_path())
        except MessageStorageError as error:
            st.warning(str(error), icon=":material/warning:")
            return None
    archive = st.session_state[ARCHIVE_KEY]
    return archive if isinstance(archive, MessageArchive) else None


def forget_archive() -> None:
    """Delete the stored messages, so nobody counts as having interacted until a new load."""
    try:
        forget_stored_messages(database_path())
    except MessageStorageError as error:
        st.error(str(error), icon=":material/error:")
        return
    # None, not absent: absent would restore from the database again.
    st.session_state[ARCHIVE_KEY] = None
    st.rerun()


def load_archive(token: str, extra_ids: list[str], only_extra: bool = False) -> None:
    """Read messages from Zalo into the session, then redraw every tab with them."""
    progress = st.empty()
    try:
        with (
            ZaloClient(token) as client,
            st.spinner("Đang tải tin nhắn từ Zalo…"),
        ):
            if only_extra:
                archive = MessageArchive(
                    tuple(fetch_conversation(client, user_id) for user_id in extra_ids)
                )
            else:

                def show_progress(text: str) -> None:
                    progress.caption(text)

                # Read only what is newer than the kept messages, and look again at
                # everyone who ever wrote: Zalo's recent list can miss quiet people.
                kept = loaded_archive()
                known_ids = frozenset(
                    message.message_id
                    for conversation in (kept.conversations if kept else ())
                    for message in conversation.messages
                )
                kept_users = [c.user_id for c in kept.conversations] if kept else []
                archive = fetch_all_messages(
                    client, [*extra_ids, *kept_users], show_progress, known_ids
                )
    except ZaloError as error:
        st.error(error_text(error), icon=":material/error:")
        return
    finally:
        progress.empty()
    try:
        # Kept in the database too, so a page refresh does not forget who interacted.
        archive = store_archive(database_path(), archive)
    except MessageStorageError as error:
        st.warning(f"{error} Tin vừa tải chỉ dùng được đến khi tải lại trang.")
    st.session_state[ARCHIVE_KEY] = archive
    # Other tabs and the notice bar were drawn before this load; draw them again.
    st.rerun()


def rescan_button(key: str) -> None:
    """Read new messages from Zalo on demand, with when they were last read beside it."""
    archive = loaded_archive()
    token = access_token()
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button(
            "Quét lại tin nhắn" if archive else "Quét tin nhắn",
            icon=":material/refresh:",
            disabled=not token,
            help=None if token else TOKEN_MISSING,
            key=key,
        ):
            load_archive(token, [])
        if archive is not None:
            st.caption(
                "Tin nhắn quét lần gần nhất lúc "
                f"{format_vietnam_time(epoch_ms(archive.fetched_at))}. Bấm quét lại để "
                "biết ai vừa nhắn cho OA."
            )


__all__ = [
    "error_text",
    "send_failure_text",
]
