"""Upload production workbooks, check them, then review and send each employee's output for a day.

The page follows the operator's steps in order: upload, pick the day, clear the problems, then
choose recipients and read their messages before sending.
"""

import hashlib
import html
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

import streamlit as st

from gui_san_luong.config import database_path, import_cache_path
from gui_san_luong.domain.production_templates import (
    TEMPLATES,
    FieldKind,
    ReportTemplate,
)
from gui_san_luong.services.employee_roster import (
    ROSTER_SHEETS,
    Roster,
    fill_full_names,
    read_roster,
)
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
from gui_san_luong.services.import_cache import cache_key, cached, file_digest
from gui_san_luong.services.output_changes import (
    ChangeStorageError,
    OutputChange,
    TrackedFile,
    VersionOutcome,
    load_open_changes,
    mark_changes_handled,
    record_version,
    source_key,
)
from gui_san_luong.services.output_dispatch import RecipientState, SendStyle
from gui_san_luong.services.output_image import OutputImageError, render_output_images
from gui_san_luong.services.worker_output import (
    CompanyShare,
    CreditedOutput,
    CreditProblem,
    EmployeeDay,
    IncompleteIdentity,
    TextStyle,
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
from gui_san_luong.ui.output_send_section import plan_day, render_output_send
from gui_san_luong.ui.zalo_free_window import render_free_window_notice
from gui_san_luong.ui.zalo_links_page import render_zalo_links
from gui_san_luong.ui.zalo_messages_page import render_zalo_messages
from gui_san_luong.ui.zalo_token_section import render_token_warning

PREVIEW_TABLE_ROWS = 200
SAMPLE_WIDTH = 24
NOT_SELECTED = -1
SEND_DAY_KEY = "send_day"
SELECT_ALL_KEY = "select_all"
PREVIEWS_KEY = "previews"
SUGGESTIONS_KEY = "sheet_suggestions"
RESULTS_KEY = "results"
ROSTERS_KEY = "rosters"
DIGESTS_KEY = "digests"
# Versions already compared with the history, so a rerun does not compare them again.
TRACKED_KEY = "tracked_versions"
CHANGED_COLUMN = "Vừa sửa"
HANDLED_COLUMN = "Đã xử lý"
CHANGES_TABLE_HEIGHT = 300
SEND_COLUMN = "Gửi"
CODE_COLUMN = "Mã NV"
NAME_COLUMN = "Họ tên"
OUTPUT_COLUMN = "Sản lượng trong ngày"
DEPARTMENTS_COLUMN = "Công đoạn trong kỳ"
TRANSFERRED_COLUMN = "Đổi công đoạn"
ZALO_COLUMN = "Zalo"
EMPLOYEE_TABLE_HEIGHT = 520
MESSAGE_HEIGHT = 470
# Workbooks already processed in this session; a new one shows the waiting screen from the start.
PROCESSED_FILES_KEY = "processed_files"
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
  display: flex; flex-direction: column; gap: 12px; padding: 20px 24px;
  margin: 0 16px; width: 440px; max-width: calc(100vw - 32px); box-sizing: border-box;
  border-radius: 8px; background: #fff; box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2);
  font-size: 1rem; color: #31333f;
}}
.busy-head {{ display: flex; align-items: center; gap: 12px; }}
.busy-spinner {{
  flex: none; width: 22px; height: 22px; border-radius: 50%;
  border: 3px solid #d0d0d6; border-top-color: #ff4b4b;
  animation: busy-spin 0.8s linear infinite;
}}
.busy-title {{ font-weight: 600; overflow-wrap: anywhere; }}
.busy-message {{ font-size: 0.9rem; overflow-wrap: anywhere; }}
.busy-hint {{ font-size: 0.8rem; color: #6b7280; }}
.busy-track {{ height: 6px; border-radius: 3px; background: #e5e7eb; overflow: hidden; }}
.busy-bar {{ height: 100%; background: #ff4b4b; transition: width 0.3s; }}
@keyframes busy-spin {{ to {{ transform: rotate(360deg); }} }}
</style>
<div class="busy-overlay" role="alert" aria-busy="true">
  <div class="busy-box">
    <div class="busy-head"><div class="busy-spinner"></div>
      <span class="busy-title">{title}</span></div>
    {progress}
    <span class="busy-message">{message}</span>
    <span class="busy-hint">Vui lòng chờ, không thao tác cho đến khi xong.</span>
  </div>
</div>
"""
BUSY_PROGRESS = '<div class="busy-track"><div class="busy-bar" style="width: {percent}%"></div></div>'


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
    template_label: str = ""
    sheet: str = ""
    template_key: str = ""
    file_digest: str = ""
    # How the file was read; a new reading of the same file is not an edit of its figures.
    read_settings: str = ""


class Severity(StrEnum):
    # Output would be wrong or reach nobody.
    ERROR = "error"
    # Output may be wrong; worth a look before sending.
    WARNING = "warning"
    # Nothing is wrong; shown so the operator knows.
    INFO = "info"


SEVERITY_ICONS = {
    Severity.ERROR: ":material/error:",
    Severity.WARNING: ":material/warning:",
    Severity.INFO: ":material/info:",
}


@dataclass(frozen=True)
class Issue:
    severity: Severity
    title: str
    detail: str
    rows: tuple[dict[str, Any], ...] = ()


def _count(number: int) -> str:
    """Vietnamese thousands separator, matching the numbers in the messages."""
    return f"{number:,}".replace(",", ".")


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
    for name in (PREVIEWS_KEY, SUGGESTIONS_KEY, RESULTS_KEY, ROSTERS_KEY, DIGESTS_KEY):
        cache = _session_cache(name)
        for key in list(cache):
            owner = key[0] if isinstance(key, tuple) else key
            if owner not in file_ids:
                del cache[key]


class BusyScreen:
    """The waiting screen over the whole page while uploaded workbooks are processed.

    For newly uploaded files it stays up from the first file until the page is ready, with the
    file being processed and a progress bar. Otherwise it appears only while a slow read runs,
    so ordinary clicks do not make the page flicker.
    """

    def __init__(self, placeholder: Any, new_upload: bool) -> None:
        self._placeholder = placeholder
        self._new_upload = new_upload
        self._title = "Đang xử lý file Excel"
        self._total = 0
        self._done = 0

    def show(self, message: str) -> None:
        progress = (
            BUSY_PROGRESS.format(percent=round(100 * self._done / self._total))
            if self._new_upload and self._total
            else ""
        )
        # File names come from the user's disk, so they are escaped before going into HTML.
        self._placeholder.markdown(
            BUSY_OVERLAY.format(
                title=html.escape(self._title),
                progress=progress,
                message=html.escape(message),
            ),
            unsafe_allow_html=True,
        )

    def checking_uploads(self) -> None:
        if self._new_upload:
            self.show("Đang kiểm tra các file vừa tải lên...")

    def start_file(self, number: int, total: int, name: str) -> None:
        self._total = total
        self._done = number - 1
        self._title = f"File {number}/{self._total}: {name}"
        if self._new_upload:
            self.show("Đang chuẩn bị đọc file...")

    def summarizing(self) -> None:
        self._done = self._total
        self._title = f"Đã đọc {self._total} file"
        if self._new_upload:
            self.show("Đang tổng hợp sản lượng theo nhân viên...")


@contextmanager
def _busy_screen(files: Sequence[UploadedWorkbook]) -> Iterator[BusyScreen]:
    """Always cleared at the end, so the page never stays locked even after an error."""
    processed: set[str] = st.session_state.setdefault(PROCESSED_FILES_KEY, set())
    current = {file.file_id for file in files}
    placeholder = st.empty()
    screen = BusyScreen(placeholder, new_upload=not current <= processed)
    try:
        yield screen
        st.session_state[PROCESSED_FILES_KEY] = current
    finally:
        placeholder.empty()


def _step(number: int, title: str, caption: str) -> None:
    st.subheader(f"{number}. {title}", anchor=False)
    st.caption(caption)


# ---------------------------------------------------------------- reading one file


def _file_digest(file: UploadedWorkbook) -> str:
    return _remember(DIGESTS_KEY, file.file_id, lambda: file_digest(file.getvalue()))


def _read_once[T](key: str, busy: BusyScreen, message: str, read: Callable[[], T]) -> T:
    """Reuse what was read from the same content before, even in an earlier session."""

    def read_with_notice() -> T:
        busy.show(message)
        return read()

    return cached(import_cache_path(), key, read_with_notice)


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
        return ":green[:material/check: Tự nhận]"
    if match.status is MatchStatus.AMBIGUOUS:
        letters = ", ".join(column_letter(column) for column in match.candidates)
        return f":orange[:material/help: Không chắc ({letters}), hãy chọn]"
    return (
        ":red[:material/error: Chưa thấy, hãy chọn]"
        if required
        else ":gray[Không có (không bắt buộc)]"
    )


def _select_template(file_name: str, file_id: str) -> ReportTemplate | None:
    suggested = suggest_template(file_name, TEMPLATES)
    return st.selectbox(
        "Loại báo cáo",
        TEMPLATES,
        index=TEMPLATES.index(suggested) if suggested else None,
        format_func=lambda template: template.label,
        placeholder="Chọn loại báo cáo",
        key=f"template:{file_id}",
        help="Tự nhận theo tên file. Đổi lại nếu nhận sai.",
    )


def _select_sheet(
    previews: list[SheetPreview], template: ReportTemplate, file_id: str
) -> tuple[SheetPreview | None, bool]:
    """The chosen sheet, and whether it was found automatically."""
    suggested = _remember(
        SUGGESTIONS_KEY,
        (file_id, template.key),
        lambda: suggest_sheet(previews, template),
    )
    names = [preview.name for preview in previews]
    by_name = {preview.name: preview for preview in previews}
    name = st.selectbox(
        "Sheet chứa số liệu",
        names,
        index=names.index(suggested) if suggested else None,
        format_func=lambda n: f"{n} (đang ẩn)" if by_name[n].hidden else n,
        placeholder="Chọn sheet",
        key=f"sheet:{file_id}:{template.key}",
        help="Tự nhận sheet có đủ các cột bắt buộc.",
    )
    return (by_name[name] if name is not None else None), suggested is not None


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
            ":blue[:material/edit: Bạn đã đổi]"
            if changed
            else _status_text(match, spec.required)
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
                value = row[spec.key]
                # Codes mix numbers and text ("002960", "cty"); a table column needs one type.
                if spec.kind is FieldKind.CODE and value is not None:
                    value = str(value)
                shown[spec.label] = value
        for label in extra_labels:
            shown[label] = None if row[label] is None else str(row[label])
        if template.duplicate_key:
            shown["Tính vào tổng"] = row[COUNTED_KEY]
        table.append(shown)
    return table


def _date_columns(template: ReportTemplate) -> dict[str, Any]:
    label = template.field(template.date_field).label
    return {label: st.column_config.DateColumn(label, format="DD/MM/YYYY")}


def _total_check(matches: bool | None, stated: float | None) -> str:
    if matches is None:
        return "— File không có số tổng"
    if matches:
        return "✅ Khớp"
    return f"⚠️ Lệch (file ghi {format_quantity(stated or 0.0)})"


def _show_totals(result: ImportResult, template: ReportTemplate) -> None:
    st.dataframe(
        [
            {
                "Cột": total.label,
                "Tổng đã đọc": format_quantity(total.counted),
                "Đối chiếu với tổng trong file": _total_check(
                    total.matches_stated, total.stated
                ),
            }
            for total in result.totals
        ],
        hide_index=True,
        width="stretch",
    )
    repeated = [total for total in result.totals if total.all_rows != total.counted]
    for total in repeated:
        st.caption(
            f"{total.label}: cộng thô mọi dòng là {format_quantity(total.all_rows)}."
        )
    if result.repeated_rows:
        st.info(
            f"{_count(result.repeated_rows)} dòng lặp lại cùng sản lượng cho từng công nhân "
            "nên chỉ được tính một lần. Khóa so sánh: "
            + ", ".join(template.field(key).label for key in template.duplicate_key),
            icon=":material/info:",
        )


def _show_result(
    result: ImportResult, template: ReportTemplate, extra_labels: list[str]
) -> None:
    valid, rejected, blank, totals = st.columns(4, border=True)
    valid.metric("Dòng đọc được", _count(len(result.rows)))
    rejected.metric("Dòng lỗi", _count(len(result.rejected)))
    blank.metric("Dòng trống bỏ qua", _count(result.blank_rows))
    totals.metric("Dòng tổng bỏ qua", _count(result.total_rows))
    _show_totals(result, template)

    if result.rejected:
        st.error(
            "Có dòng không đọc được nên chưa được tính. Hãy sửa trong file Excel rồi tải lại.",
            icon=":material/error:",
        )
        st.dataframe(
            [
                {"Dòng Excel": item.row, "Lý do": item.reason}
                for item in result.rejected
            ],
            hide_index=True,
            width="stretch",
        )

    by_group, details = st.tabs(
        ["Tổng theo ngày", f"Chi tiết ({_count(PREVIEW_TABLE_ROWS)} dòng đầu)"]
    )
    columns = _date_columns(template)
    by_group.dataframe(
        result.groups, hide_index=True, width="stretch", column_config=columns
    )
    details.dataframe(
        _display_rows(result, template, extra_labels),
        hide_index=True,
        width="stretch",
        column_config=columns,
    )


def _credit(
    result: ImportResult,
    template: ReportTemplate,
    mapping: dict[str, int],
    file_name: str,
    sheet: str,
) -> FileOutcome:
    slots = template.worker_slots
    unmapped = [slot.role for slot in slots if not is_slot_mapped(slot, mapping)]
    if len(unmapped) == len(slots):
        reason = "sheet đang chọn không có cột mã số nhân viên hoặc số lượng"
        st.warning(
            f"File này chưa vào được tổng hợp nhân viên: {reason}.",
            icon=":material/warning:",
        )
        return FileOutcome(
            file_name,
            result,
            excluded_because=reason,
            template_label=template.label,
            sheet=sheet,
        )
    if unmapped:
        st.warning(
            "Chưa đủ cột nên không tính phần của: " + ", ".join(unmapped),
            icon=":material/warning:",
        )
    return FileOutcome(
        file_name,
        result,
        credit_workers(result, template, mapping, file_name),
        template_label=template.label,
        sheet=sheet,
    )


def _render_file(file: UploadedWorkbook, busy: BusyScreen) -> FileOutcome:
    data = file.getvalue()
    digest = _file_digest(file)

    def load() -> list[SheetPreview]:
        return _read_once(
            cache_key("previews", digest),
            busy,
            f"Đang đọc danh sách sheet của {file.name}...",
            lambda: read_previews(data),
        )

    try:
        previews = _remember(PREVIEWS_KEY, file.file_id, load)
    except ExcelImportError as error:
        st.error(str(error), icon=":material/error:")
        return FileOutcome(file.name, excluded_because=str(error))

    template_col, sheet_col, start_col = st.columns([3, 3, 2])
    with template_col:
        template = _select_template(file.name, file.file_id)
    if template is None:
        st.info("Hãy chọn loại báo cáo cho file này.", icon=":material/info:")
        return FileOutcome(file.name, excluded_because="chưa chọn loại báo cáo")
    with sheet_col:
        preview, sheet_found = _select_sheet(previews, template, file.file_id)
    if not sheet_found:
        st.warning(
            "Không tự tìm được sheet có đủ cột bắt buộc. Hãy tự chọn sheet và cột.",
            icon=":material/warning:",
        )
    if preview is None:
        return FileOutcome(
            file.name,
            excluded_because="chưa chọn sheet",
            template_label=template.label,
        )

    scope = f"{file.file_id}:{template.key}:{preview.name}"
    detected = analyze_sheet(preview, template).data_start_row
    with start_col:
        start_row = int(
            st.number_input(
                "Dữ liệu bắt đầu từ dòng",
                min_value=2,
                max_value=PREVIEW_ROWS,
                value=detected or HEADER_SCAN_ROWS,
                key=f"start:{scope}",
                help="Tự nhận theo dòng đầu tiên có ngày thật. Các dòng phía trên là tiêu đề.",
            )
        )
    if detected is None:
        st.warning(
            "Không tự tìm được dòng bắt đầu dữ liệu. Hãy nhập đúng số dòng.",
            icon=":material/warning:",
        )

    layout = analyze_sheet(preview, template, start_row)
    options = _column_options(preview, layout, start_row)
    needs_attention = any(
        spec.required and layout.matches[spec.key].status is not MatchStatus.AUTO
        for spec in template.fields
    )
    with st.expander(
        "Cột lấy số liệu" + (" — cần chọn lại" if needs_attention else ""),
        expanded=needs_attention,
        icon=":material/warning:" if needs_attention else ":material/view_column:",
    ):
        st.caption(
            "Cột được tự nhận theo tiêu đề nên vẫn đúng khi bị xê dịch. "
            "Bạn có thể đổi lại bất kỳ cột nào. Dấu :red[*] là cột bắt buộc."
        )
        mapping = _select_mapping(template, layout, options, scope)
        extra_columns = st.multiselect(
            "Lấy thêm cột khác để xem",
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
        st.warning(
            "Có hai trường đang dùng chung một cột. Hãy kiểm tra lại.",
            icon=":material/warning:",
        )

    signature = (
        scope,
        start_row,
        tuple(sorted(mapping.items())),
        tuple(sorted(extras)),
    )

    settings = (
        preview.name,
        template.key,
        start_row,
        tuple(sorted(mapping.items())),
        tuple(sorted(extras.items())),
    )

    def read() -> ImportResult:
        return _read_once(
            cache_key("rows", digest, settings),
            busy,
            f"Đang đọc dữ liệu {file.name}...",
            lambda: read_rows(data, preview.name, template, mapping, start_row, extras),
        )

    try:
        result = _remember(RESULTS_KEY, (file.file_id, signature), read)
    except ExcelImportError as error:
        st.error(str(error), icon=":material/error:")
        return FileOutcome(
            file.name,
            excluded_because=str(error),
            template_label=template.label,
            sheet=preview.name,
        )

    outcome = _credit(result, template, mapping, file.name, preview.name)
    _show_result(result, template, list(extras))
    return replace(
        outcome,
        template_key=template.key,
        file_digest=digest,
        read_settings=repr(settings),
    )


def _keep_unique_files(
    files: Sequence[UploadedWorkbook],
) -> list[UploadedWorkbook]:
    """The same workbook uploaded twice would double everyone's output."""
    unique: dict[str, UploadedWorkbook] = {}
    for file in files:
        if file.size > MAX_FILE_BYTES:
            limit = MAX_FILE_BYTES // (1024 * 1024)
            st.error(
                f"Bỏ qua {file.name}: file vượt quá {limit} MB.",
                icon=":material/error:",
            )
            continue
        digest = _file_digest(file)
        if digest in unique:
            st.warning(
                f"Bỏ qua {file.name}: trùng nội dung với {unique[digest].name}.",
                icon=":material/warning:",
            )
            continue
        unique[digest] = file
    return list(unique.values())


def _file_state(outcome: FileOutcome) -> str:
    if outcome.excluded_because:
        return f"⛔ Chưa tính: {outcome.excluded_because.rstrip('.')}"
    result = outcome.result
    if result is None:
        return "⛔ Chưa đọc được"
    notes = []
    if result.rejected:
        notes.append(f"{_count(len(result.rejected))} dòng lỗi")
    if any(total.matches_stated is False for total in result.totals):
        notes.append("lệch tổng trong file")
    return "⚠️ " + ", ".join(notes) if notes else "✅ Sẵn sàng"


def _show_file_status(outcomes: list[FileOutcome]) -> None:
    st.dataframe(
        [
            {
                "File": outcome.file_name,
                "Loại báo cáo": outcome.template_label,
                "Sheet": outcome.sheet,
                "Dòng đọc được": (
                    _count(len(outcome.result.rows)) if outcome.result else ""
                ),
                "Tình trạng": _file_state(outcome),
            }
            for outcome in outcomes
        ],
        hide_index=True,
        width="stretch",
    )
    if any(not _file_state(outcome).startswith("✅") for outcome in outcomes):
        st.caption(
            "File chưa sẵn sàng: mở “Xem và chỉnh cách đọc từng file” bên dưới để xem lý do."
        )


def _load_roster(file: UploadedWorkbook, busy: BusyScreen) -> Roster | None:
    def load() -> Roster | None:
        return _read_once(
            cache_key("roster", _file_digest(file)),
            busy,
            f"Đang đọc họ tên nhân viên trong {file.name}...",
            lambda: read_roster(file.getvalue()),
        )

    try:
        return _remember(ROSTERS_KEY, file.file_id, load)
    except ExcelImportError:
        # The file's own tab already says it cannot be read.
        return None


# ---------------------------------------------------------------- problems to check


def _incomplete_identities_issue(people: list[IncompleteIdentity]) -> Issue | None:
    if not people:
        return None
    without_code = sum(person.missing_code for person in people)
    title = f"{_count(len(people))} người chưa có đủ họ tên hoặc mã số"
    if without_code:
        title += f" ({_count(without_code)} người thiếu mã, không nhận được sản lượng)"
    return Issue(
        Severity.ERROR if without_code else Severity.WARNING,
        title,
        "Tính trên mọi file và mọi ngày. Người thiếu mã số không nhận được sản lượng; "
        "người chỉ có tên gọi tắt vẫn nhận được nhưng khó kiểm tra đúng người. "
        "Họ tên đã được tra theo mã số trong sheet BANG CONG (hoặc BCC) của mỗi file; "
        "người ở đây chưa có trong sheet đó. Hãy bổ sung trong file Excel rồi tải lại.",
        tuple(
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
        ),
    )


def _roster_issues(rosters: dict[str, Roster | None]) -> list[Issue]:
    issues = [
        Issue(
            Severity.WARNING,
            f"Sheet {roster.sheet} của {name} ghi hai họ tên cho mã "
            + ", ".join(roster.conflicting_codes),
            f"Sheet {roster.sheet} ghi hai họ tên khác nhau cho cùng một mã, nên chưa dùng "
            "họ tên của mã này. Hãy sửa cho thống nhất rồi tải lại file.",
        )
        for name, roster in sorted(rosters.items())
        if roster and roster.conflicting_codes
    ]
    missing = sorted(name for name, roster in rosters.items() if roster is None)
    if missing:
        sheets = " hoặc ".join(name.upper() for name in ROSTER_SHEETS)
        issues.append(
            Issue(
                Severity.INFO,
                f"{_count(len(missing))} file không có sheet {sheets} để tra họ tên",
                ", ".join(missing),
            )
        )
    return issues


def _file_issues(outcomes: list[FileOutcome]) -> list[Issue]:
    issues = []
    for outcome in outcomes:
        if outcome.excluded_because:
            reason = outcome.excluded_because.rstrip(".")
            issues.append(
                Issue(
                    Severity.ERROR,
                    f"Chưa tính file {outcome.file_name}",
                    f"Lý do: {reason}. Hãy chỉnh ở phần 1.",
                )
            )
            continue
        if outcome.result is None:
            continue
        if outcome.result.rejected:
            issues.append(
                Issue(
                    Severity.ERROR,
                    f"{outcome.file_name}: {_count(len(outcome.result.rejected))} dòng lỗi "
                    "chưa được tính",
                    "Hãy sửa trong file Excel rồi tải lại.",
                    tuple(
                        {"Dòng Excel": item.row, "Lý do": item.reason}
                        for item in outcome.result.rejected
                    ),
                )
            )
        mismatched = [
            total.label
            for total in outcome.result.totals
            if total.matches_stated is False
        ]
        if mismatched:
            issues.append(
                Issue(
                    Severity.WARNING,
                    f"{outcome.file_name}: lệch với tổng trong file",
                    f"Các cột bị lệch: {', '.join(mismatched)}. "
                    "Xem chi tiết trong tab của file ở phần 1.",
                )
            )
    return issues


def _day_issues(
    outputs: list[WorkerOutput], problems: list[CreditProblem], day: date
) -> list[Issue]:
    issues = [
        Issue(
            Severity.WARNING,
            f"Bộ phận {department} có số liệu ngày này ở nhiều file",
            "Sản lượng đang được cộng từ tất cả các file: " + ", ".join(sources),
        )
        for department, sources in find_day_overlaps(outputs, day).items()
    ]

    day_problems = [problem for problem in problems if problem.day == day]
    if day_problems:
        issues.append(
            Issue(
                Severity.ERROR,
                f"{_count(len(day_problems))} phần sản lượng ngày này chưa gán được "
                "cho nhân viên",
                "Các phần này không có trong danh sách gửi bên dưới.",
                tuple(
                    {
                        "File": problem.source,
                        "Dòng Excel": problem.excel_row,
                        "Lý do": problem.reason,
                    }
                    for problem in day_problems
                ),
            )
        )

    conflicts = find_name_conflicts(outputs)
    if conflicts:
        issues.append(
            Issue(
                Severity.WARNING,
                f"{_count(len(conflicts))} mã số gắn với nhiều họ tên khác nhau",
                "Có thể chỉ là gõ khác nhau, nhưng cũng có thể hai người dùng chung mã nên "
                "sản lượng bị gộp. Hãy kiểm tra trước khi gửi.",
                tuple(
                    {
                        CODE_COLUMN: code,
                        NAME_COLUMN: variant.name,
                        "Bộ phận": ", ".join(variant.departments),
                        "File Excel": ", ".join(variant.sources),
                    }
                    for code, variants in sorted(conflicts.items())
                    for variant in variants
                ),
            )
        )
    return issues


def _company_share_issue(shares: list[CompanyShare], day: date) -> Issue | None:
    day_shares = [share for share in shares if share.day == day]
    if not day_shares:
        return None
    return Issue(
        Severity.INFO,
        f"{_count(len(day_shares))} phần ghi cho công ty, không gửi cho ai",
        "Vẫn được tính trong tổng của file.",
        tuple(
            {
                "File": share.source,
                "Dòng Excel": share.excel_row,
                "Công đoạn": share.department,
                "Số lượng": format_quantity(share.quantity),
                "Đơn vị": share.unit or "",
            }
            for share in day_shares
        ),
    )


def _show_issues(issues: list[Issue]) -> None:
    order = list(Severity)
    issues = sorted(issues, key=lambda issue: order.index(issue.severity))
    errors = sum(issue.severity is Severity.ERROR for issue in issues)
    warnings = sum(issue.severity is Severity.WARNING for issue in issues)
    if errors:
        st.error(
            f"Có {_count(errors)} vấn đề làm sản lượng không đến đúng người"
            + (f" và {_count(warnings)} điểm nên xem lại" if warnings else "")
            + ". Hãy xử lý trước khi gửi.",
            icon=":material/error:",
        )
    elif warnings:
        st.warning(
            f"Có {_count(warnings)} điểm nên xem lại trước khi gửi.",
            icon=":material/warning:",
        )
    else:
        st.success(
            "Không phát hiện vấn đề nào. Có thể chuyển sang bước gửi.",
            icon=":material/check_circle:",
        )
    for issue in issues:
        with st.expander(issue.title, icon=SEVERITY_ICONS[issue.severity]):
            st.caption(issue.detail)
            if issue.rows:
                st.dataframe(list(issue.rows), hide_index=True, width="stretch")


# ---------------------------------------------------------------- edited figures


def _track_changes(outcomes: list[FileOutcome], outputs: list[WorkerOutput]) -> None:
    """Compare each uploaded report with its last version and announce what was edited."""
    tracked: set[tuple[str, str, str]] = st.session_state.setdefault(TRACKED_KEY, set())
    by_source: defaultdict[str, list[WorkerOutput]] = defaultdict(list)
    for output in outputs:
        by_source[output.source].append(output)
    edited: list[str] = []
    found = 0
    for outcome in outcomes:
        if outcome.credited is None or not outcome.file_digest:
            continue
        key = source_key(outcome.template_key, outcome.file_name)
        version = (key, outcome.file_digest, outcome.read_settings)
        if version in tracked:
            continue
        try:
            recorded = record_version(
                database_path(),
                TrackedFile(
                    key,
                    outcome.file_name,
                    outcome.file_digest,
                    by_source[outcome.file_name],
                ),
            )
        except ChangeStorageError as error:
            st.error(str(error), icon=":material/error:")
            return
        tracked.add(version)
        if recorded.outcome is VersionOutcome.CHANGED and recorded.changes:
            found += recorded.changes
            edited.append(outcome.file_name)
    if found:
        st.toast(
            f"{_count(found)} dòng sản lượng khác lần tải trước trong "
            f"{', '.join(edited)}. Xem mục “Sản lượng vừa sửa”.",
            icon=":material/edit_note:",
            duration="long",
        )


def _load_changes() -> list[OutputChange]:
    try:
        return load_open_changes(database_path())
    except ChangeStorageError as error:
        st.error(str(error), icon=":material/error:")
        return []


def _with_unit(quantity: float | None, unit: str | None) -> str:
    if quantity is None:
        return "—"
    text = format_quantity(quantity)
    return f"{text} {unit}" if unit else text


def _signed(quantity: float) -> str:
    text = format_quantity(abs(quantity))
    return f"+{text}" if quantity > 0 else f"−{text}"


def _changes_table(changes: list[OutputChange]) -> list[dict[str, Any]]:
    return [
        {
            "id": change.id,
            HANDLED_COLUMN: False,
            "Ngày": change.day,
            "Dòng Excel": change.excel_row,
            CODE_COLUMN: change.employee_code,
            NAME_COLUMN: change.employee_name,
            "Nội dung": change.line,
            "Trước": _with_unit(change.old_quantity, change.unit),
            "Sau": _with_unit(change.new_quantity, change.unit),
            "Chênh lệch": _signed(change.difference),
            "File": change.file_name,
            "Phát hiện lúc": change.detected_at.astimezone().replace(tzinfo=None),
        }
        for change in changes
    ]


def _mark_handled(change_ids: list[int]) -> None:
    try:
        mark_changes_handled(database_path(), change_ids)
    except ChangeStorageError as error:
        st.error(str(error), icon=":material/error:")
        return
    st.rerun()


def _show_changes(changes: list[OutputChange]) -> None:
    if not changes:
        st.caption(
            ":material/history: Chưa có sản lượng nào khác lần tải trước. Khi bạn sửa file "
            "Excel rồi tải lại, người bị đổi số liệu sẽ hiện ở đây để gửi lại tin."
        )
        return
    people = {(change.day, change.employee_code) for change in changes}
    with st.container(border=True):
        st.markdown("**:material/notifications_active: Sản lượng vừa sửa**")
        st.warning(
            f"{_count(len(people))} lượt nhân viên có số liệu khác lần tải trước "
            f"({_count(len(changes))} dòng). Hãy gửi lại tin cho họ (ở bước 4 có nút "
            "“Chỉ chọn người vừa sửa”), rồi đánh dấu đã xử lý.",
            icon=":material/edit_note:",
        )
        ids = "|".join(str(change.id) for change in changes)
        table = st.data_editor(
            _changes_table(changes),
            hide_index=True,
            width="stretch",
            height=min(CHANGES_TABLE_HEIGHT, 38 + 35 * len(changes)),
            column_config={
                "id": None,
                HANDLED_COLUMN: st.column_config.CheckboxColumn(
                    HANDLED_COLUMN,
                    width="small",
                    help="Tích khi đã gửi lại tin cho người này.",
                ),
                "Ngày": st.column_config.DateColumn("Ngày", format="DD/MM/YYYY"),
                "Dòng Excel": st.column_config.NumberColumn(
                    "Dòng Excel", format="%d", width="small"
                ),
                "Phát hiện lúc": st.column_config.DatetimeColumn(
                    "Phát hiện lúc", format="HH:mm DD/MM/YYYY"
                ),
                "Nội dung": st.column_config.TextColumn("Nội dung", width="large"),
            },
            disabled=[
                "Ngày",
                "Dòng Excel",
                CODE_COLUMN,
                NAME_COLUMN,
                "Nội dung",
                "Trước",
                "Sau",
                "Chênh lệch",
                "File",
                "Phát hiện lúc",
            ],
            key=f"changes:{hashlib.sha256(ids.encode()).hexdigest()}",
        )
        ticked = [int(row["id"]) for row in table if row[HANDLED_COLUMN]]
        with st.container(horizontal=True):
            if st.button(
                f"Lưu {_count(len(ticked))} dòng đã xử lý",
                icon=":material/done:",
                disabled=not ticked,
            ):
                _mark_handled(ticked)
            if st.button("Đánh dấu tất cả đã xử lý", icon=":material/done_all:"):
                _mark_handled([change.id for change in changes])


# ---------------------------------------------------------------- recipients and messages


def _select_day(days: list[date]) -> date | None:
    # A day kept from files since removed would fall outside the allowed range and raise.
    stored = st.session_state.get(SEND_DAY_KEY)
    if isinstance(stored, date) and not days[0] <= stored <= days[-1]:
        del st.session_state[SEND_DAY_KEY]
    chosen = st.date_input(
        "Ngày sản xuất",
        value=days[-1],
        min_value=days[0],
        max_value=days[-1],
        format="DD/MM/YYYY",
        key=SEND_DAY_KEY,
        help=f"Các file có số liệu từ {days[0]:%d/%m/%Y} đến {days[-1]:%d/%m/%Y}.",
    )
    return chosen if isinstance(chosen, date) else None


class Selection(StrEnum):
    ALL = "all"
    NONE = "none"
    CHANGED = "changed"


def _changed_note(changes: list[OutputChange]) -> str:
    if not changes:
        return ""
    if len(changes) == 1:
        change = changes[0]
        return (
            f"✏️ {_with_unit(change.old_quantity, change.unit)} → "
            f"{_with_unit(change.new_quantity, change.unit)}"
        )
    return f"✏️ {_count(len(changes))} dòng đổi"


def _employee_table(
    employees: list[EmployeeDay],
    zalo_labels: dict[str, str],
    selection: Selection,
    changed: dict[str, list[OutputChange]],
) -> list[dict[str, Any]]:
    return [
        {
            SEND_COLUMN: selection is Selection.ALL
            or (selection is Selection.CHANGED and employee.employee_code in changed),
            CODE_COLUMN: employee.employee_code,
            NAME_COLUMN: employee.employee_name,
            CHANGED_COLUMN: _changed_note(changed.get(employee.employee_code, [])),
            ZALO_COLUMN: zalo_labels.get(employee.employee_code, "Chưa nối"),
            OUTPUT_COLUMN: " · ".join(format_line(line) for line in employee.lines),
            DEPARTMENTS_COLUMN: ", ".join(employee.period_departments),
            TRANSFERRED_COLUMN: employee.was_transferred,
        }
        for employee in employees
    ]


def _show_messages(employee: EmployeeDay) -> None:
    """Exactly what the "Gửi dạng chữ" button sends."""
    messages = build_messages(employee, TextStyle.EMOJI)
    if len(messages) > 1:
        st.caption(f"Nội dung dài nên sẽ chia thành {len(messages)} tin.")
    for index, message in enumerate(messages, start=1):
        with st.container(border=True):
            if len(messages) > 1:
                st.caption(f"Tin {index}/{len(messages)}")
            st.text(message)


def _show_person_preview(selected: list[EmployeeDay], day: date) -> None:
    employee = st.selectbox(
        "Xem tin của",
        selected,
        format_func=lambda e: f"{e.employee_code} — {e.employee_name}",
        key=f"message_preview:{day.isoformat()}",
    )
    if employee is None:
        return
    with st.container(height=MESSAGE_HEIGHT, border=False):
        _show_messages(employee)


def _show_image_preview(selected: list[EmployeeDay], day: date) -> None:
    """Exactly what the "Gửi dạng ảnh màu" button sends, drawn for one person at a time."""
    employee = st.selectbox(
        "Xem ảnh của",
        selected,
        format_func=lambda e: f"{e.employee_code} — {e.employee_name}",
        key=f"image_preview:{day.isoformat()}",
    )
    if employee is None:
        return
    try:
        images = render_output_images(employee)
    except OutputImageError as error:
        st.error(str(error), icon=":material/error:")
        return
    with st.container(height=MESSAGE_HEIGHT, border=False):
        for index, image in enumerate(images, start=1):
            if len(images) > 1:
                st.caption(f"Ảnh {index}/{len(images)}")
            st.image(image.data, width="stretch")
            st.caption(f"Chữ kèm ảnh: {image.caption}")


def _show_department_preview(selected: list[EmployeeDay], day: date) -> None:
    groups = group_by_department(selected)
    department = st.selectbox(
        "Bộ phận",
        list(groups),
        format_func=lambda name: f"{name} ({_count(len(groups[name]))} người)",
        key=f"department_preview:{day.isoformat()}",
        help=(
            "Người làm nhiều bộ phận trong ngày nhận một tin gồm sản lượng của mọi bộ phận, "
            "nên có mặt ở mỗi bộ phận đó."
        ),
    )
    if department is None:
        return
    with st.container(height=MESSAGE_HEIGHT, border=False):
        for employee in groups[department]:
            st.markdown(f"**{employee.employee_code} — {employee.employee_name}**")
            _show_messages(employee)


def _selection_buttons(day: date, changed_people: int) -> Selection:
    """Which rows start ticked; the buttons rebuild the table with that default."""
    key = f"{SELECT_ALL_KEY}:{day.isoformat()}"
    selection = Selection(st.session_state.get(key, Selection.ALL))
    with st.container(horizontal=True):
        if st.button("Chọn tất cả", icon=":material/select_all:", key=f"all:{key}"):
            selection = st.session_state[key] = Selection.ALL
        if st.button("Bỏ chọn tất cả", icon=":material/deselect:", key=f"none:{key}"):
            selection = st.session_state[key] = Selection.NONE
        if changed_people and st.button(
            f"Chỉ chọn người vừa sửa ({_count(changed_people)})",
            icon=":material/edit_note:",
            key=f"changed:{key}",
            help="Những người có số liệu khác lần tải file trước, cần gửi lại tin.",
        ):
            selection = st.session_state[key] = Selection.CHANGED
    return selection


class SentFilter(StrEnum):
    ALL = "all"
    NOT_SENT = "not_sent"
    SENT = "sent"


def _sent_filter(day: date, sent: int, total: int) -> SentFilter:
    labels = {
        SentFilter.ALL: f"Tất cả ({_count(total)})",
        SentFilter.NOT_SENT: f"Chưa gửi ({_count(total - sent)})",
        SentFilter.SENT: f"Đã gửi ({_count(sent)})",
    }
    chosen = st.segmented_control(
        "Hiện",
        list(SentFilter),
        format_func=labels.__getitem__,
        default=SentFilter.ALL,
        key=f"send_filter:{day.isoformat()}",
        help=(
            "“Đã gửi” là người đã nhận đúng nội dung hiện tại. Người bị ẩn không được "
            "gửi, kể cả khi đang được chọn."
        ),
    )
    return SentFilter(chosen) if chosen else SentFilter.ALL


# Ticking recipients reruns only this section: rerunning the whole page redraws every file's
# tables, which takes most of a second once several workbooks are loaded.
@st.fragment
def _show_employees(
    employees: list[EmployeeDay],
    day: date,
    links: dict[str, str],
    changed: dict[str, list[OutputChange]],
) -> None:
    zalo_labels = _zalo_labels(employees, links, day)
    sent_label = ZALO_STATE_LABELS[RecipientState.ALREADY_SENT]
    sent_codes = {code for code, label in zalo_labels.items() if label == sent_label}
    summary = st.container()
    table_col, preview_col = st.columns([3, 2], gap="large")
    with table_col:
        view = _sent_filter(day, len(sent_codes), len(employees))
        shown = [
            employee
            for employee in employees
            if view is SentFilter.ALL
            or (view is SentFilter.SENT) == (employee.employee_code in sent_codes)
        ]
        selection = _selection_buttons(
            day, sum(employee.employee_code in changed for employee in shown)
        )
        # Edits are kept by row position, so a changed list needs a new key or a tick lands
        # on another person.
        roster = "|".join(employee.employee_code for employee in shown)
        digest = hashlib.sha256(roster.encode()).hexdigest()
        table = st.data_editor(
            _employee_table(shown, zalo_labels, selection, changed),
            hide_index=True,
            width="stretch",
            height=EMPLOYEE_TABLE_HEIGHT,
            column_config={
                SEND_COLUMN: st.column_config.CheckboxColumn(
                    SEND_COLUMN, width="small", help="Bỏ chọn người không cần gửi."
                ),
                CODE_COLUMN: st.column_config.TextColumn(CODE_COLUMN, width="small"),
                NAME_COLUMN: st.column_config.TextColumn(NAME_COLUMN, width="medium"),
                OUTPUT_COLUMN: st.column_config.TextColumn(
                    OUTPUT_COLUMN, width="large"
                ),
                TRANSFERRED_COLUMN: st.column_config.CheckboxColumn(
                    TRANSFERRED_COLUMN,
                    width="small",
                    help="Làm ở nhiều công đoạn trong kỳ.",
                ),
                ZALO_COLUMN: st.column_config.TextColumn(ZALO_COLUMN, width="small"),
                CHANGED_COLUMN: st.column_config.TextColumn(
                    CHANGED_COLUMN,
                    width="small",
                    help="Số liệu khác lần tải file trước và chưa đánh dấu đã xử lý.",
                ),
            },
            disabled=[
                CODE_COLUMN,
                NAME_COLUMN,
                OUTPUT_COLUMN,
                DEPARTMENTS_COLUMN,
                TRANSFERRED_COLUMN,
                ZALO_COLUMN,
                CHANGED_COLUMN,
            ],
            key=f"send_table:{day.isoformat()}:{digest}:{selection}:{view}",
        )
    selected_codes = {row[CODE_COLUMN] for row in table if row[SEND_COLUMN]}
    # Only rows in view can be selected, so a filtered-out person is never sent to.
    selected = [
        employee for employee in shown if employee.employee_code in selected_codes
    ]
    not_linked = sum(e.employee_code not in links for e in selected)

    with summary:
        count, chosen, moved, missing = st.columns(4, border=True)
        count.metric("Có sản lượng ngày này", _count(len(employees)))
        chosen.metric("Đang chọn gửi", _count(len(selected)))
        moved.metric(
            "Đổi công đoạn trong kỳ",
            _count(sum(employee.was_transferred for employee in employees)),
        )
        missing.metric("Đang chọn mà chưa nối Zalo", _count(not_linked))

    with preview_col:
        st.markdown("**Xem trước tin nhắn**")
        if not selected:
            st.info(
                "Chưa chọn ai. Tích cột “Gửi” để xem trước tin.", icon=":material/info:"
            )
        else:
            by_person, by_department, as_image = st.tabs(
                ["Theo nhân viên", "Theo bộ phận", "Ảnh màu"]
            )
            with by_person:
                _show_person_preview(selected, day)
            with by_department:
                _show_department_preview(selected, day)
            with as_image:
                _show_image_preview(selected, day)

    st.divider()
    render_output_send(selected, links, day, database_path())


ZALO_STATE_LABELS = {
    RecipientState.NOT_LINKED: "Chưa nối",
    RecipientState.NO_INTERACTION: "🚫 Chưa tương tác",
    RecipientState.ALREADY_SENT: "✅ Đã gửi",
    RecipientState.UNKNOWN_BEFORE: "❓ Không rõ",
}


def _zalo_labels(
    employees: list[EmployeeDay], links: dict[str, str], day: date
) -> dict[str, str]:
    """Whether each person is linked, and whether today's exact message already went."""
    labels: dict[str, str] = {}
    # Text first, so a person sent in either style shows as sent.
    for style in (SendStyle.IMAGE, SendStyle.TEXT):
        for plan in plan_day(employees, links, day, database_path(), style=style) or []:
            label = ZALO_STATE_LABELS.get(plan.state, "Đã nối")
            if (
                labels.get(plan.employee.employee_code)
                != ZALO_STATE_LABELS[RecipientState.ALREADY_SENT]
            ):
                labels[plan.employee.employee_code] = label
    return labels


def _links() -> dict[str, str]:
    try:
        return {
            link.employee_code: link.zalo_user_id
            for link in load_links(database_path())
        }
    except ZaloLinkStorageError as error:
        st.error(str(error), icon=":material/error:")
        return {}


# ---------------------------------------------------------------- page


def _render_summary(
    outcomes: list[FileOutcome], rosters: dict[str, Roster | None]
) -> list[WorkerOutput]:
    outputs = fill_full_names(
        (
            output
            for outcome in outcomes
            if outcome.credited
            for output in outcome.credited.outputs
        ),
        {name: roster for name, roster in rosters.items() if roster},
    )
    _track_changes(outcomes, outputs)
    changes = _load_changes()
    _show_changes(changes)
    problems = [
        problem
        for outcome in outcomes
        if outcome.credited
        for problem in outcome.credited.problems
    ]
    issues = [
        *_file_issues(outcomes),
        *_roster_issues(rosters),
    ]
    if incomplete := _incomplete_identities_issue(
        find_incomplete_identities(outputs, problems)
    ):
        issues.append(incomplete)

    st.divider()
    _step(
        2,
        "Chọn ngày gửi",
        "Sản lượng được gộp theo mã số nhân viên trên mọi file, nên người đổi công đoạn "
        "vẫn là một người. Sản lượng khác đơn vị không được cộng chung.",
    )
    days = available_days(outputs)
    if not days:
        st.info(
            "Chưa có sản lượng nào gán được cho nhân viên. Hãy xem tình trạng file ở phần 1.",
            icon=":material/info:",
        )
        _show_issues(issues)
        return outputs

    day_col, _ = st.columns([1, 3])
    with day_col:
        day = _select_day(days)
    if day is None:
        return outputs

    issues.extend(_day_issues(outputs, problems, day))
    if company := _company_share_issue(
        [
            share
            for outcome in outcomes
            if outcome.credited
            for share in outcome.credited.company_shares
        ],
        day,
    ):
        issues.append(company)

    st.divider()
    _step(
        3,
        "Kiểm tra trước khi gửi",
        "Những điểm có thể làm sai số liệu hoặc gửi nhầm người. Bấm vào từng mục để xem chi tiết.",
    )
    _show_issues(issues)

    st.divider()
    _step(
        4,
        f"Chọn người nhận và xem trước tin ngày {day:%d/%m/%Y}",
        "Bỏ chọn người không cần gửi. Tin nhắn bên phải đúng như nội dung sẽ gửi qua Zalo.",
    )
    employees = summarize_day(outputs, day)
    if not employees:
        st.info(
            f"Không có sản lượng ngày {day:%d/%m/%Y}. Hãy chọn ngày khác.",
            icon=":material/info:",
        )
        return outputs
    changed: defaultdict[str, list[OutputChange]] = defaultdict(list)
    for change in changes:
        if change.day == day:
            changed[change.employee_code].append(change)
    _show_employees(employees, day, _links(), dict(changed))
    return outputs


def _show_getting_started() -> None:
    with st.container(border=True):
        st.markdown(
            "**Cách dùng**\n\n"
            "1. Tải các file báo cáo sản lượng Excel của tháng (chọn được nhiều file).\n"
            "2. Chọn ngày cần gửi.\n"
            "3. Xem và xử lý các điểm cần kiểm tra.\n"
            "4. Chọn người nhận, xem trước tin rồi gửi."
        )


def _render_production() -> list[WorkerOutput]:
    _step(
        1,
        "Tải file báo cáo",
        "Loại báo cáo, sheet và cột được tự nhận. File chỉ được đọc, không bị sửa hay lưu lại.",
    )
    uploaded = st.file_uploader(
        "Chọn hoặc kéo thả các file Excel (.xlsx, .xlsm)",
        type=["xlsx", "xlsm"],
        accept_multiple_files=True,
    )
    _forget_removed_files({file.file_id for file in uploaded})
    if not uploaded:
        _show_getting_started()
        return []

    # Created before anything is read and kept up until the summary is drawn, so the slow first
    # pass over large workbooks is covered from start to finish. It stays outside the expander,
    # which hides its content when closed.
    with _busy_screen(uploaded) as busy:
        busy.checking_uploads()
        files = _keep_unique_files(uploaded)
        status = st.container()
        needs_setup = any(
            suggest_template(file.name, TEMPLATES) is None for file in files
        )
        outcomes = []
        rosters: dict[str, Roster | None] = {}
        with st.expander(
            "Xem và chỉnh cách đọc từng file",
            expanded=needs_setup,
            icon=":material/tune:",
        ):
            tabs = st.tabs([file.name for file in files])
            for number, (tab, file) in enumerate(zip(tabs, files, strict=True), 1):
                busy.start_file(number, len(files), file.name)
                with tab:
                    outcomes.append(_render_file(file, busy))
                    rosters[file.name] = _load_roster(file, busy)
        busy.summarizing()
        with status:
            _show_file_status(outcomes)
        return _render_summary(outcomes, rosters)


def main() -> None:
    st.set_page_config(
        page_title="Gửi sản lượng", page_icon=":material/send:", layout="wide"
    )
    st.title("Gửi sản lượng", anchor=False)
    st.caption(
        "Tổng hợp sản lượng từ file Excel và gửi cho từng nhân viên qua Zalo. "
        "Trang chưa có đăng nhập nên chỉ chạy trên máy cục bộ."
    )
    render_token_warning()
    render_free_window_notice(database_path())
    # Tabs rather than separate pages: Streamlit drops the state of widgets on a page that is
    # not showing, which would discard the uploads and any column the user corrected.
    production, zalo_links, zalo_messages = st.tabs(
        [
            ":material/inventory: Sản lượng",
            ":material/link: Nối Zalo",
            ":material/forum: Tin nhắn Zalo (thử)",
        ]
    )
    with production:
        outputs = _render_production()
    with zalo_links:
        render_zalo_links(outputs, database_path())
    with zalo_messages:
        render_zalo_messages()


main()
