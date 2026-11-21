"""Let the operator enter each employee's Zalo user id, now or whenever the list is available."""

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

import streamlit as st

from gui_san_luong.services.worker_output import WorkerOutput, list_employees
from gui_san_luong.services.zalo_links import (
    EditableRow,
    InvalidLinksError,
    LinkRow,
    ZaloLinkStorageError,
    build_editable_rows,
    load_links,
    save_links,
)
from gui_san_luong.ui.zalo_code_proposals import render_code_proposals
from gui_san_luong.ui.zalo_state import loaded_archive

# There is no login yet, so changes cannot be attributed to a person.
LOCAL_ACTOR = "local"
CODE_COLUMN = "Mã NV"
NAME_COLUMN = "Họ tên"
ZALO_COLUMN = "Zalo user id"
DEPARTMENT_COLUMN = "Công đoạn"
REMOVE_COLUMN = "Gỡ nối"
LINKED_ONLY_KEY = "zalo_links_linked_only"
SAVED_NOTICE_KEY = "zalo_links_saved_notice"


def _editor_key(rows: list[EditableRow]) -> str:
    # Edits are kept by row position, so a changed table needs a new key or a typed id would
    # land on another employee.
    digest = hashlib.sha256(repr(rows).encode()).hexdigest()
    return f"zalo_links:{digest}"


def _save(
    table: Sequence[Mapping[str, object]],
    db_path: Path,
) -> None:
    rows = [
        LinkRow(
            row.get(CODE_COLUMN),
            row.get(NAME_COLUMN),
            # Ticking "Gỡ nối" is the same as clearing the id: the link is removed.
            "" if row.get(REMOVE_COLUMN) else row.get(ZALO_COLUMN),
        )
        for row in table
    ]
    try:
        changes = save_links(db_path, rows, LOCAL_ACTOR)
    except InvalidLinksError as error:
        st.error("Chưa lưu gì vì có dòng cần sửa:")
        for problem in error.problems:
            st.text(problem)
    except ZaloLinkStorageError as error:
        st.error(str(error))
    else:
        st.session_state[SAVED_NOTICE_KEY] = (
            f"Đã lưu {len(changes.upserts):,} dòng, gỡ {len(changes.removals):,} dòng."
        )
        st.rerun()


def render_zalo_links(outputs: Sequence[WorkerOutput], db_path: Path) -> None:
    st.subheader("Nối mã nhân viên với Zalo", anchor=False)
    st.caption(
        "Danh sách gồm nhân viên trong các file đã tải và những người đã nối trước đó. "
        "Bạn có thể thêm dòng hoặc dán nhiều ô một lần. Để gỡ, tích “Gỡ nối” (hoặc xóa Zalo "
        "user id) rồi lưu; sau đó có thể nối lại bằng tay hoặc từ tin nhắn ở trên. "
        "Nối ở đây chưa phải là cho phép gửi số liệu."
    )
    notice = st.session_state.pop(SAVED_NOTICE_KEY, None)
    if notice:
        st.success(notice)

    try:
        stored = load_links(db_path)
    except ZaloLinkStorageError as error:
        st.error(str(error))
        return

    roster = list_employees(outputs)
    render_code_proposals(loaded_archive(), stored, roster, db_path)
    st.divider()

    all_rows = build_editable_rows(stored, roster)
    # Linked people first: they are the ones checked, corrected or removed most often.
    all_rows.sort(key=lambda row: (not row.zalo_user_id, row.employee_code))
    linked, waiting = st.columns(2, border=True)
    linked.metric("Đã nối", f"{len(stored):,}".replace(",", "."))
    waiting.metric(
        "Chưa nối",
        f"{sum(not row.zalo_user_id for row in all_rows):,}".replace(",", "."),
    )
    linked_only = st.toggle("Chỉ hiện người đã nối", key=LINKED_ONLY_KEY)
    rows = [row for row in all_rows if row.zalo_user_id] if linked_only else all_rows
    # The filter hides only unlinked rows, which have nothing to save.

    table = st.data_editor(
        [
            {
                CODE_COLUMN: row.employee_code,
                NAME_COLUMN: row.employee_name,
                ZALO_COLUMN: row.zalo_user_id,
                DEPARTMENT_COLUMN: row.departments,
                REMOVE_COLUMN: False,
            }
            for row in rows
        ]
        or [
            {
                CODE_COLUMN: "",
                NAME_COLUMN: "",
                ZALO_COLUMN: "",
                DEPARTMENT_COLUMN: "",
                REMOVE_COLUMN: False,
            }
        ],
        column_config={
            # Text columns, because long ids lose digits when treated as numbers.
            CODE_COLUMN: st.column_config.TextColumn(CODE_COLUMN, required=False),
            NAME_COLUMN: st.column_config.TextColumn(NAME_COLUMN),
            ZALO_COLUMN: st.column_config.TextColumn(ZALO_COLUMN),
            DEPARTMENT_COLUMN: st.column_config.TextColumn(
                DEPARTMENT_COLUMN, disabled=True
            ),
            REMOVE_COLUMN: st.column_config.CheckboxColumn(
                REMOVE_COLUMN,
                width="small",
                help="Tích rồi bấm “Lưu thay đổi” để bỏ Zalo user id của người này.",
            ),
        },
        hide_index=True,
        width="stretch",
        num_rows="dynamic",
        key=f"{_editor_key(rows)}:{linked_only}",
    )
    if st.button(
        "Lưu thay đổi", type="primary", icon=":material/save:", key="save_zalo_links"
    ):
        _save(table, db_path)
