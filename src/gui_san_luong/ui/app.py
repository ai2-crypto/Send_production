"""Upload production workbooks, one tab each, then review output per employee for a chosen day."""

import hashlib
import html
from collections.abc import Callable, Hashable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import streamlit as st

from gui_san_luong.config import database_path
from gui_san_luong.domain.production_templates import TEMPLATES, ReportTemplate
from gui_san_luong.services.excel_import import (
    COUNTED_KEY,
    HEADER_SCAN_ROWS,
    MAX_FILE_BYTES,
    PREVIEW_ROWS,
    ROW_KEY,
    ExcelImportError,
    FieldMatch,
    ImportResult,
    MatchStatus,
    SheetLayout,
    SheetPreview,
    analyze_sheet,
    column_letter,
    read_previews,
    read_rows,
    suggest_sheet,
    suggest_template,
)
from gui_san_luong.services.worker_output import (
    CompanyShare,
    CreditedOutput,
    CreditProblem,
    EmployeeDay,
    IncompleteIdentity,
    WorkerOutput,
    available_days,
    build_messages,
    credit_workers,
    find_day_overlaps,
    find_incomplete_identities,
    find_name_conflicts,
    format_line,
    format_quantity,
    group_by_department,
    is_slot_mapped,
    summarize_day,
)
from gui_san_luong.services.zalo_links import ZaloLinkStorageError, load_links
from gui_san_luong.ui.zalo_links_page import render_zalo_links

PREVIEW_TABLE_ROWS = 200
SAMPLE_WIDTH = 24
NOT_SELECTED = -1
SEND_DAY_KEY = "send_day"
PREVIEWS_KEY = "previews"
SUGGESTIONS_KEY = "sheet_suggestions"
RESULTS_KEY = "results"
SEND_COLUMN = "Gửi"
CODE_COLUMN = "Mã NV"
ZALO_COLUMN = "Zalo"
MESSAGE_LIST_HEIGHT = 600
# Covers the whole page while a workbook is read, because a click during the read reruns
# the script and starts the slow read over again.
BUSY_OVERLAY = """
<style>
.busy-overlay {{
  position: fixed; inset: 0; z-index: 1000000;
  display: flex; align-items: center; justify-content: center;
  background: rgba(255, 255, 255, 0.75); cursor: wait;
}}
.busy-box {{
  display: flex; align-items: center; gap: 12px; padding: 16px 24px;
  margin: 0 16px; max-width: 480px;
  border-radius: 8px; background: #fff; box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2);
  font-size: 1rem; color: #31333f;
}}
.busy-spinner {{
  width: 22px; height: 22px; border-radius: 50%;
  border: 3px solid #d0d0d6; border-top-color: #ff4b4b;
  animation: busy-spin 0.8s linear infinite;
}}
@keyframes busy-spin {{ to {{ transform: rotate(360deg); }} }}
</style>
<div class="busy-overlay" role="alert" aria-busy="true">
  <div class="busy-box"><div class="busy-spinner"></div><span>{message}</span></div>
</div>
"""


class UploadedWorkbook(Protocol):
    name: str
    size: int
    file_id: str

    def getvalue(self) -> bytes: ...


@dataclass(frozen=True)
class FileOutcome:
    file_name: str
    result: ImportResult | None = None
    credited: CreditedOutput | None = None
    # Why this file is left out of the employee summary.
    excluded_because: str | None = None


def _session_cache(name: str) -> dict[Hashable, Any]:
    # Kept per session rather than in st.cache_data so one user's data never reaches another.
    cache: dict[Hashable, Any] = st.session_state.setdefault(name, {})
    return cache


def _remember[T](name: str, key: Hashable, compute: Callable[[], T]) -> T:
    cache = _session_cache(name)
    if key not in cache:
        cache[key] = compute()
    value: T = cache[key]
    return value


def _forget_removed_files(file_ids: set[str]) -> None:
    for name in (PREVIEWS_KEY, SUGGESTIONS_KEY, RESULTS_KEY):
        cache = _session_cache(name)
        for key in list(cache):
            owner = key[0] if isinstance(key, tuple) else key
            if owner not in file_ids:
                del cache[key]


def _show_busy(notice: Any, message: str) -> None:
    # File names come from the user's disk, so they are escaped before going into HTML.
    notice.markdown(
        BUSY_OVERLAY.format(
            message=html.escape(
                f"{message} Vui lòng chờ, không thao tác cho đến khi xong."
            )
        ),
        unsafe_allow_html=True,
    )


@contextmanager
def _busy_area() -> Iterator[Any]:
    """A placeholder for the loading overlay, always cleared so the page never stays locked."""
    notice = st.empty()
    try:
        yield notice
    finally:
        notice.empty()


def _sample_value(preview: SheetPreview, column: int, start_row: int) -> str:
    for row in preview.rows[start_row - 1 :]:
        if column < len(row) and row[column] not in (None, ""):
            value = row[column]
            text = (
                value.strftime("%d/%m/%Y")
                if isinstance(value, datetime)
                else str(value)
            )
            return text[:SAMPLE_WIDTH]
    return ""


def _column_options(
    preview: SheetPreview, layout: SheetLayout, start_row: int
) -> dict[int, str]:
    width = max((len(row) for row in preview.rows), default=0)
    matched = {match.column for match in layout.matches.values()}
    options = {NOT_SELECTED: "(Không lấy)"}
    for column in range(width):
        header = layout.headers.get(column, "")
        sample = _sample_value(preview, column, start_row)
        if header or sample or column in matched:
            example = f"  ·  vd: {sample}" if sample else ""
            options[column] = (
                f"{column_letter(column)} — {header or '(không tiêu đề)'}{example}"
            )
    return options


def _status_text(match: FieldMatch, required: bool) -> str:
    if match.status is MatchStatus.AUTO:
        return ":green[Tự nhận]"
    if match.status is MatchStatus.AMBIGUOUS:
        letters = ", ".join(column_letter(column) for column in match.candidates)
        return f":orange[Không chắc ({letters}), hãy chọn]"
    return (
        ":red[Chưa thấy, hãy chọn]"
        if required
        else ":gray[Không thấy (không bắt buộc)]"
    )


def _select_template(file_name: str, file_id: str) -> ReportTemplate | None:
    suggested = suggest_template(file_name, TEMPLATES)
    return st.selectbox(
        "Loại báo cáo",
        TEMPLATES,
        index=TEMPLATES.index(suggested) if suggested else None,
        format_func=lambda template: template.label,
        placeholder="Hãy chọn loại báo cáo",
        key=f"template:{file_id}",
    )


def _select_sheet(
    previews: list[SheetPreview], template: ReportTemplate, file_id: str
) -> SheetPreview | None:
    suggested = _remember(
        SUGGESTIONS_KEY,
        (file_id, template.key),
        lambda: suggest_sheet(previews, template),
    )
    names = [preview.name for preview in previews]
    by_name = {preview.name: preview for preview in previews}
    name = st.selectbox(
        "Sheet",
        names,
        index=names.index(suggested) if suggested else None,
        format_func=lambda n: f"{n} (đang ẩn)" if by_name[n].hidden else n,
        placeholder="Hãy chọn sheet",
        key=f"sheet:{file_id}:{template.key}",
    )
    if suggested is None:
        st.warning(
            "Không tự tìm được sheet có đủ cột bắt buộc. Hãy tự chọn sheet và cột."
        )
    elif name == suggested:
        st.caption("Sheet này được tự nhận vì có đủ các cột bắt buộc.")
    return by_name[name] if name is not None else None


def _select_mapping(
    template: ReportTemplate,
    layout: SheetLayout,
    options: dict[int, str],
    scope: str,
) -> dict[str, int]:
    mapping: dict[str, int] = {}
    choices = list(options)
    for spec in template.fields:
        match = layout.matches[spec.key]
        default = match.column if match.column in options else NOT_SELECTED
        label_col, select_col, status_col = st.columns(
            [2, 5, 3], vertical_alignment="center"
        )
        label_col.markdown(f"**{spec.label}**" + (" :red[*]" if spec.required else ""))
        column = select_col.selectbox(
            spec.label,
            choices,
            index=choices.index(default if default is not None else NOT_SELECTED),
            format_func=options.__getitem__,
            key=f"field:{scope}:{spec.key}",
            label_visibility="collapsed",
        )
        changed = column != (NOT_SELECTED if match.column is None else match.column)
        status_col.markdown(
            ":blue[Bạn đã đổi]" if changed else _status_text(match, spec.required)
        )
        if column != NOT_SELECTED:
            mapping[spec.key] = column
    return mapping


def _display_rows(
    result: ImportResult, template: ReportTemplate, extra_labels: list[str]
) -> list[dict[str, Any]]:
    table = []
    for row in result.rows[:PREVIEW_TABLE_ROWS]:
        shown: dict[str, Any] = {"Dòng Excel": row[ROW_KEY]}
        for spec in template.fields:
            if spec.key in row:
                shown[spec.label] = row[spec.key]
        for label in extra_labels:
            shown[label] = None if row[label] is None else str(row[label])
        if template.duplicate_key:
            shown["Tính vào tổng"] = row[COUNTED_KEY]
        table.append(shown)
    return table


def _show_totals(result: ImportResult, template: ReportTemplate) -> None:
    for total in result.totals:
        message = f"**{total.label}:** {total.counted:,.2f}"
        if total.all_rows != total.counted:
            message += f" (cộng thô mọi dòng là {total.all_rows:,.2f})"
        if total.matches_stated is None:
            st.markdown(message + " · :gray[file không có số tổng để đối chiếu]")
        elif total.matches_stated:
            st.markdown(
                message + f" · :green[khớp tổng trong file ({total.stated:,.2f})]"
            )
        else:
            st.markdown(
                message + f" · :red[LỆCH với tổng trong file ({total.stated:,.2f})]"
            )
    if result.repeated_rows:
        st.info(
            f"{result.repeated_rows:,} dòng lặp lại cùng sản lượng cho từng công nhân nên chỉ "
            f"được tính một lần. Khóa so sánh: "
            + ", ".join(template.field(key).label for key in template.duplicate_key)
        )


def _show_result(
    result: ImportResult, template: ReportTemplate, extra_labels: list[str]
) -> None:
    st.subheader("Kết quả đọc")
    valid, rejected, blank, totals = st.columns(4)
    valid.metric("Dòng hợp lệ", f"{len(result.rows):,}")
    rejected.metric("Dòng bị từ chối", f"{len(result.rejected):,}")
    blank.metric("Dòng trống bỏ qua", f"{result.blank_rows:,}")
    totals.metric("Dòng tổng bỏ qua", f"{result.total_rows:,}")
    _show_totals(result, template)

    if result.rejected:
        st.error("Có dòng không đọc được. Các dòng này không được tính vào tổng.")
        st.dataframe(
            [
                {"Dòng Excel": item.row, "Lý do": item.reason}
                for item in result.rejected
            ],
            hide_index=True,
        )

    by_group, details = st.tabs(
        ["Tổng theo ngày", f"Chi tiết ({PREVIEW_TABLE_ROWS} dòng đầu)"]
    )
    by_group.dataframe(result.groups, hide_index=True)
    details.dataframe(_display_rows(result, template, extra_labels), hide_index=True)


def _credit(
    result: ImportResult,
    template: ReportTemplate,
    mapping: dict[str, int],
    file_name: str,
) -> FileOutcome:
    slots = template.worker_slots
    unmapped = [slot.role for slot in slots if not is_slot_mapped(slot, mapping)]
    if len(unmapped) == len(slots):
        reason = "sheet đang chọn không có cột mã số nhân viên hoặc số lượng"
        st.warning(f"File này chưa vào được tổng hợp nhân viên: {reason}.")
        return FileOutcome(file_name, result, excluded_because=reason)
    if unmapped:
        st.warning("Chưa đủ cột nên không tính phần của: " + ", ".join(unmapped))
    return FileOutcome(
        file_name, result, credit_workers(result, template, mapping, file_name)
    )


def _render_file(file: UploadedWorkbook, notice: Any) -> FileOutcome:
    data = file.getvalue()

    def load() -> list[SheetPreview]:
        _show_busy(notice, f"Đang đọc danh sách sheet của {file.name}...")
        return read_previews(data)

    try:
        previews = _remember(PREVIEWS_KEY, file.file_id, load)
    except ExcelImportError as error:
        st.error(str(error))
        return FileOutcome(file.name, excluded_because=str(error))

    template_col, sheet_col = st.columns(2)
    with template_col:
        template = _select_template(file.name, file.file_id)
    if template is None:
        return FileOutcome(file.name, excluded_because="chưa chọn loại báo cáo")
    with sheet_col:
        preview = _select_sheet(previews, template, file.file_id)
    if preview is None:
        return FileOutcome(file.name, excluded_because="chưa chọn sheet")

    scope = f"{file.file_id}:{template.key}:{preview.name}"
    detected = analyze_sheet(preview, template).data_start_row
    start_row = int(
        st.number_input(
            "Dữ liệu bắt đầu từ dòng",
            min_value=2,
            max_value=PREVIEW_ROWS,
            value=detected or HEADER_SCAN_ROWS,
            key=f"start:{scope}",
            help="Tự nhận theo dòng đầu tiên có ngày thật. Các dòng phía trên được coi là tiêu đề.",
        )
    )
    if detected is None:
        st.warning("Không tự tìm được dòng bắt đầu dữ liệu. Hãy nhập đúng số dòng.")

    layout = analyze_sheet(preview, template, start_row)
    options = _column_options(preview, layout, start_row)
    needs_attention = any(
        spec.required and layout.matches[spec.key].status is not MatchStatus.AUTO
        for spec in template.fields
    )
    with st.expander("Cột cần lấy", expanded=needs_attention):
        st.caption(
            "Cột được tự nhận theo tiêu đề nên vẫn đúng khi bị xê dịch. "
            "Bạn có thể đổi lại bất kỳ cột nào."
        )
        mapping = _select_mapping(template, layout, options, scope)
        extra_columns = st.multiselect(
            "Lấy thêm cột khác",
            [
                column
                for column in options
                if column != NOT_SELECTED and column not in mapping.values()
            ],
            format_func=options.__getitem__,
            key=f"extras:{scope}",
        )
    extras = {
        f"{column_letter(column)} — {layout.headers.get(column, '')}".strip(
            " —"
        ): column
        for column in extra_columns
    }

    columns_used = list(mapping.values())
    if len(columns_used) != len(set(columns_used)):
        st.warning("Có hai trường đang dùng chung một cột. Hãy kiểm tra lại.")

    signature = (
        scope,
        start_row,
        tuple(sorted(mapping.items())),
        tuple(sorted(extras)),
    )

    def read() -> ImportResult:
        _show_busy(notice, f"Đang đọc dữ liệu {file.name}...")
        return read_rows(data, preview.name, template, mapping, start_row, extras)

    try:
        result = _remember(RESULTS_KEY, (file.file_id, signature), read)
    except ExcelImportError as error:
        st.error(str(error))
        return FileOutcome(file.name, excluded_because=str(error))

    outcome = _credit(result, template, mapping, file.name)
    _show_result(result, template, list(extras))
    return outcome


def _keep_unique_files(
    files: Sequence[UploadedWorkbook],
) -> list[UploadedWorkbook]:
    """The same workbook uploaded twice would double everyone's output."""
    unique: dict[str, UploadedWorkbook] = {}
    for file in files:
        if file.size > MAX_FILE_BYTES:
            limit = MAX_FILE_BYTES // (1024 * 1024)
            st.error(f"Bỏ qua {file.name}: file vượt quá {limit} MB.")
            continue
        digest = hashlib.sha256(file.getvalue()).hexdigest()
        if digest in unique:
            st.warning(f"Bỏ qua {file.name}: trùng nội dung với {unique[digest].name}.")
            continue
        unique[digest] = file
    return list(unique.values())


def _select_day(days: list[date]) -> date | None:
    # A day kept from files since removed would fall outside the allowed range and raise.
    stored = st.session_state.get(SEND_DAY_KEY)
    if isinstance(stored, date) and not days[0] <= stored <= days[-1]:
        del st.session_state[SEND_DAY_KEY]
    chosen = st.date_input(
        "Ngày gửi sản lượng",
        value=days[-1],
        min_value=days[0],
        max_value=days[-1],
        format="DD/MM/YYYY",
        key=SEND_DAY_KEY,
    )
    return chosen if isinstance(chosen, date) else None


def _show_warnings(
    outcomes: list[FileOutcome],
    outputs: list[WorkerOutput],
    problems: list[CreditProblem],
    day: date,
) -> None:
    for outcome in outcomes:
        if outcome.excluded_because:
            st.warning(
                f"Chưa tính file {outcome.file_name}: {outcome.excluded_because.rstrip('.')}."
            )
        if outcome.result is None or outcome.excluded_because:
            continue
        if outcome.result.rejected:
            st.warning(
                f"{outcome.file_name}: {len(outcome.result.rejected):,} dòng bị từ chối "
                "chưa được tính. Xem ở tab của file."
            )
        mismatched = [
            total.label
            for total in outcome.result.totals
            if total.matches_stated is False
        ]
        if mismatched:
            st.warning(
                f"{outcome.file_name}: lệch với tổng trong file ở "
                + ", ".join(mismatched)
            )

    for department, sources in find_day_overlaps(outputs, day).items():
        st.warning(
            f"Bộ phận {department} có số liệu ngày này ở nhiều file, sản lượng đang được "
            "cộng từ tất cả: " + ", ".join(sources)
        )

    day_problems = [problem for problem in problems if problem.day == day]
    if day_problems:
        st.error(
            f"{len(day_problems):,} phần sản lượng trong ngày này chưa gán được cho nhân viên "
            "và không có trong bảng dưới."
        )
        st.dataframe(
            [
                {
                    "File": problem.source,
                    "Dòng Excel": problem.excel_row,
                    "Lý do": problem.reason,
                }
                for problem in day_problems
            ],
            hide_index=True,
        )

    conflicts = find_name_conflicts(outputs)
    if conflicts:
        st.warning(
            "Có mã số gắn với nhiều họ tên khác nhau. Có thể là gõ khác nhau, nhưng cũng có thể "
            "hai người dùng chung mã nên sản lượng bị gộp. Hãy kiểm tra trước khi gửi."
        )
        st.dataframe(
            [
                {CODE_COLUMN: code, "Các họ tên": " / ".join(names)}
                for code, names in sorted(conflicts.items())
            ],
            hide_index=True,
        )


def _employee_table(
    employees: list[EmployeeDay], linked_codes: set[str]
) -> list[dict[str, Any]]:
    return [
        {
            SEND_COLUMN: True,
            CODE_COLUMN: employee.employee_code,
            "Họ tên": employee.employee_name,
            "Sản lượng trong ngày": " · ".join(
                format_line(line) for line in employee.lines
            ),
            "Công đoạn trong kỳ": ", ".join(employee.period_departments),
            "Đã đổi công đoạn": "Có" if employee.was_transferred else "",
            ZALO_COLUMN: (
                "Đã nối" if employee.employee_code in linked_codes else "Chưa nối"
            ),
        }
        for employee in employees
    ]


def _show_messages(employee: EmployeeDay) -> None:
    messages = build_messages(employee)
    if len(messages) > 1:
        st.caption(f"Nội dung dài nên sẽ chia thành {len(messages)} tin.")
    for message in messages:
        st.text(message)


def _show_department_messages(selected: list[EmployeeDay], day: date) -> None:
    groups = group_by_department(selected)
    department = st.selectbox(
        "Bộ phận",
        list(groups),
        format_func=lambda name: f"{name} ({len(groups[name]):,} người)",
        key=f"department_preview:{day.isoformat()}",
    )
    if department is None:
        return
    st.caption(
        "Danh sách tin sẽ gửi cho nhân viên đang chọn của bộ phận này. Người làm nhiều bộ "
        "phận trong ngày nhận một tin gồm sản lượng của mọi bộ phận, nên có mặt ở mỗi bộ phận đó."
    )
    with st.container(height=MESSAGE_LIST_HEIGHT):
        for employee in groups[department]:
            with st.container(border=True):
                st.text(f"Gửi cho {employee.employee_code} — {employee.employee_name}")
                _show_messages(employee)


def _show_incomplete_identities(people: list[IncompleteIdentity]) -> None:
    if not people:
        return
    without_code = sum(person.missing_code for person in people)
    with st.expander(
        f"{len(people):,} người chưa có đủ họ tên hoặc mã số",
        expanded=bool(without_code),
    ):
        st.caption(
            "Tính trên mọi file và mọi ngày. Người thiếu mã số không nhận được sản lượng; "
            "người chỉ có tên gọi tắt vẫn nhận được nhưng khó kiểm tra đúng người. "
            "Hãy bổ sung trong file Excel rồi tải lại."
        )
        st.dataframe(
            [
                {
                    CODE_COLUMN: person.employee_code or "",
                    "Tên trong file": person.employee_name,
                    "Thiếu": " và ".join(
                        part
                        for part, missing in (
                            ("mã số", person.missing_code),
                            ("họ tên đầy đủ", person.missing_full_name),
                        )
                        if missing
                    ),
                    "Bộ phận": ", ".join(person.departments),
                    "File": ", ".join(person.sources),
                    "Số dòng": person.excel_rows,
                }
                for person in people
            ],
            hide_index=True,
            width="stretch",
        )


def _show_employees(
    employees: list[EmployeeDay], day: date, linked_codes: set[str]
) -> None:
    transferred = sum(employee.was_transferred for employee in employees)
    # Edits are kept by row position, so a changed list needs a new key or a tick lands on
    # another person.
    roster = "|".join(employee.employee_code for employee in employees)
    table = st.data_editor(
        _employee_table(employees, linked_codes),
        hide_index=True,
        width="stretch",
        disabled=[
            CODE_COLUMN,
            "Họ tên",
            "Sản lượng trong ngày",
            "Công đoạn trong kỳ",
            "Đã đổi công đoạn",
            ZALO_COLUMN,
        ],
        key=f"send_table:{day.isoformat()}:{hashlib.sha256(roster.encode()).hexdigest()}",
    )
    selected_codes = {row[CODE_COLUMN] for row in table if row[SEND_COLUMN]}
    selected = [
        employee for employee in employees if employee.employee_code in selected_codes
    ]

    count, chosen, moved, missing = st.columns(4)
    count.metric("Nhân viên có sản lượng", f"{len(employees):,}")
    chosen.metric("Đang chọn gửi", f"{len(selected):,}")
    moved.metric("Đã đổi công đoạn trong kỳ", f"{transferred:,}")
    missing.metric(
        "Chưa nối Zalo",
        f"{sum(e.employee_code not in linked_codes for e in employees):,}",
    )

    if selected:
        by_person, by_department = st.tabs(
            ["Xem trước theo nhân viên", "Xem trước theo bộ phận"]
        )
        with by_person:
            employee = st.selectbox(
                "Xem trước nội dung của",
                selected,
                format_func=lambda e: f"{e.employee_code} — {e.employee_name}",
                key=f"message_preview:{day.isoformat()}",
            )
            _show_messages(employee)
        with by_department:
            _show_department_messages(selected, day)

    st.button(
        f"Gửi Zalo cho {len(selected):,} nhân viên",
        type="primary",
        disabled=True,
        help="Chưa kết nối Zalo OA.",
    )
    st.caption(
        "Chưa gửi được: ứng dụng chưa kết nối Zalo OA. Zalo user id của nhân viên nhập ở tab “Nối Zalo”."
    )


def _linked_codes() -> set[str]:
    try:
        return {link.employee_code for link in load_links(database_path())}
    except ZaloLinkStorageError as error:
        st.error(str(error))
        return set()


def _show_company_shares(shares: list[CompanyShare], day: date) -> None:
    day_shares = [share for share in shares if share.day == day]
    if not day_shares:
        return
    with st.expander(f"{len(day_shares):,} phần ghi cho công ty, không gửi cho ai"):
        st.dataframe(
            [
                {
                    "File": share.source,
                    "Dòng Excel": share.excel_row,
                    "Công đoạn": share.department,
                    "Số lượng": format_quantity(share.quantity),
                    "Đơn vị": share.unit or "",
                }
                for share in day_shares
            ],
            hide_index=True,
        )


def _render_summary(outcomes: list[FileOutcome]) -> list[WorkerOutput]:
    st.divider()
    st.header("Tổng hợp sản lượng theo nhân viên")
    st.caption(
        "Gộp theo mã số nhân viên trên mọi file, nên người đổi công đoạn trong tháng vẫn là "
        "một dòng. Sản lượng khác đơn vị không được cộng chung."
    )

    outputs = [
        output
        for outcome in outcomes
        if outcome.credited
        for output in outcome.credited.outputs
    ]
    problems = [
        problem
        for outcome in outcomes
        if outcome.credited
        for problem in outcome.credited.problems
    ]
    _show_incomplete_identities(find_incomplete_identities(outputs, problems))
    days = available_days(outputs)
    if not days:
        for outcome in outcomes:
            if outcome.excluded_because:
                st.warning(
                    f"Chưa tính file {outcome.file_name}: {outcome.excluded_because.rstrip('.')}."
                )
        st.info("Chưa có sản lượng nào gán được cho nhân viên.")
        return outputs

    day = _select_day(days)
    if day is None:
        return outputs
    st.caption(f"Dữ liệu từ {days[0]:%d/%m/%Y} đến {days[-1]:%d/%m/%Y}.")

    _show_warnings(outcomes, outputs, problems, day)
    _show_company_shares(
        [
            share
            for outcome in outcomes
            if outcome.credited
            for share in outcome.credited.company_shares
        ],
        day,
    )
    employees = summarize_day(outputs, day)
    if not employees:
        st.info(f"Không có sản lượng ngày {day:%d/%m/%Y}.")
        return outputs
    _show_employees(employees, day, _linked_codes())
    return outputs


def _render_production() -> list[WorkerOutput]:
    st.info(
        "Chỉ đọc file để tổng hợp số liệu, không lưu lại và chưa gửi Zalo. "
        "Trang chưa có đăng nhập nên chỉ chạy trên máy cục bộ."
    )
    uploaded = st.file_uploader(
        "Tải các file Excel", type=["xlsx", "xlsm"], accept_multiple_files=True
    )
    _forget_removed_files({file.file_id for file in uploaded})
    files = _keep_unique_files(uploaded)
    if not files:
        return []

    outcomes = []
    with _busy_area() as notice:
        tabs = st.tabs([file.name for file in files])
        for tab, file in zip(tabs, files, strict=True):
            with tab:
                outcomes.append(_render_file(file, notice))
    return _render_summary(outcomes)


def main() -> None:
    st.set_page_config(page_title="Gửi sản lượng", layout="wide")
    st.title("Gửi sản lượng")
    # Tabs rather than separate pages: Streamlit drops the state of widgets on a page that is
    # not showing, which would discard the uploads and any column the user corrected.
    production, zalo_links = st.tabs(["Sản lượng", "Nối Zalo"])
    with production:
        outputs = _render_production()
    with zalo_links:
        render_zalo_links(outputs, database_path())


main()
