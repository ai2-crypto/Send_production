"""Nối Zalo section: employee codes people texted to the OA, offered for the operator to apply."""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import streamlit as st

from gui_san_luong.db.zalo_links import LinkRecord
from gui_san_luong.services.worker_output import EmployeeIdentity
from gui_san_luong.services.zalo_codes import (
    LinkProposal,
    ProposalStatus,
    apply_proposals,
    latest_claims,
    plan_proposals,
)
from gui_san_luong.services.zalo_links import InvalidLinksError, ZaloLinkStorageError
from gui_san_luong.services.zalo_messages import MessageArchive
from gui_san_luong.services.zalo_texts import proposal_status_text
from gui_san_luong.ui.zalo_state import (
    epoch_ms,
    format_vietnam_time,
    rescan_button,
)

APPLY_COLUMN = "Cập nhật"
LOCAL_ACTOR = "local"
NOTICE_KEY = "zalo_code_proposals_notice"


def _table(proposals: Sequence[LinkProposal]) -> list[dict[str, Any]]:
    return [
        {
            APPLY_COLUMN: proposal.selected_by_default,
            "Tên Zalo": proposal.claim.display_name or "",
            "Mã NV": proposal.employee_code,
            "Họ tên trong danh sách": proposal.employee_name or "",
            "Tình trạng": proposal_status_text(proposal),
            "Nhắn lúc": format_vietnam_time(proposal.claim.sent_ms),
            "Tin nhắn": proposal.claim.message,
            "Zalo user id": proposal.claim.user_id,
        }
        for proposal in proposals
    ]


def _apply(
    db_path: Path, stored: Sequence[LinkRecord], chosen: list[LinkProposal]
) -> None:
    try:
        changes = apply_proposals(db_path, stored, chosen, LOCAL_ACTOR)
    except InvalidLinksError as error:
        st.error("Chưa lưu gì vì có dòng cần sửa:")
        for problem in error.problems:
            st.text(problem)
        return
    except ZaloLinkStorageError as error:
        st.error(str(error))
        return
    st.session_state[NOTICE_KEY] = (
        f"Đã cập nhật {len(changes.upserts):,} người vào danh sách nối Zalo."
    )
    st.rerun()


def render_code_proposals(
    archive: MessageArchive | None,
    stored: Sequence[LinkRecord],
    roster: Sequence[EmployeeIdentity],
    db_path: Path,
) -> None:
    st.markdown("**:material/sms: Lấy mã nhân viên từ tin nhắn Zalo**")
    st.caption(
        "Lấy mã 6 chữ số (ví dụ 000020) trong tin mới nhất mỗi người nhắn cho OA. Ai cũng "
        "có thể nhắn mã của người khác, nên hãy so tên Zalo với họ tên trước khi cập nhật. "
        "Dòng có thể nối nhầm người không được chọn sẵn."
    )
    if notice := st.session_state.pop(NOTICE_KEY, None):
        st.success(notice, icon=":material/check_circle:")
    rescan_button("rescan_zalo_codes")
    if archive is None:
        st.info(
            "Chưa có tin nhắn. Dán token ở tab “Tin nhắn Zalo (thử)” rồi bấm "
            "“Quét tin nhắn”.",
            icon=":material/info:",
        )
        return
    proposals = plan_proposals(latest_claims(archive), stored, roster)
    pending = [p for p in proposals if p.status is not ProposalStatus.UNCHANGED]
    already = len(proposals) - len(pending)
    st.caption(
        f"Theo tin nhắn tải lúc {format_vietnam_time(epoch_ms(archive.fetched_at))}. "
        f"{len(proposals):,} người có nhắn mã, {already:,} người đã nối đúng."
    )
    if not pending:
        st.info("Không có mã nào cần cập nhật.", icon=":material/info:")
        return
    digest = hashlib.sha256(repr(pending).encode()).hexdigest()
    table = st.data_editor(
        _table(pending),
        hide_index=True,
        width="stretch",
        column_config={
            APPLY_COLUMN: st.column_config.CheckboxColumn(APPLY_COLUMN, width="small"),
            "Tin nhắn": st.column_config.TextColumn("Tin nhắn", width="medium"),
        },
        disabled=[column for column in _table(pending)[0] if column != APPLY_COLUMN],
        key=f"zalo_code_proposals:{digest}",
    )
    chosen = [p for p, row in zip(pending, table, strict=True) if row[APPLY_COLUMN]]
    if st.button(
        f"Cập nhật {len(chosen):,} người vào danh sách",
        type="primary",
        icon=":material/person_add:",
        disabled=not chosen,
        key="apply_zalo_code_proposals",
    ):
        _apply(db_path, stored, chosen)
