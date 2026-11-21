"""Turn uploaded workbooks into checked, per-employee output, the same way for every caller.

Shared by the Streamlit page and the HTTP API: picking the report type, sheet, start row and
columns, crediting the rows to employees, and listing what should be checked before sending.
Nothing here draws anything or keeps state between calls.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from gui_san_luong.domain.production_templates import (
    TEMPLATES,
    FieldKind,
    ReportTemplate,
)
from gui_san_luong.services.employee_roster import (
    ROSTER_SHEETS,
    Roster,
    fill_full_names,
)
from gui_san_luong.services.excel_import import (
    COUNTED_KEY,
    HEADER_SCAN_ROWS,
    MAX_FILE_BYTES,
    PREVIEW_ROWS,
    ROW_KEY,
    ExcelImportError,
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
from gui_san_luong.services.import_cache import cache_key
from gui_san_luong.services.output_changes import (
    TrackedFile,
    VersionOutcome,
    record_version,
    source_key,
)
from gui_san_luong.services.worker_output import (
    CompanyShare,
    CreditedOutput,
    CreditProblem,
    IncompleteIdentity,
    WorkerOutput,
    credit_workers,
    find_day_overlaps,
    find_incomplete_identities,
    find_name_conflicts,
    format_quantity,
    is_slot_mapped,
)

CODE_COLUMN = "Mã NV"
NAME_COLUMN = "Họ tên"
SAMPLE_WIDTH = 24
NO_EMPLOYEE_COLUMNS = "sheet đang chọn không có cột mã số nhân viên hoặc số lượng"

# Runs a slow read once per content: (cache key, message for the waiting screen, read).
type CachedReader = Callable[[str, str, Callable[[], Any]], Any]


def count_text(number: int) -> str:
    """Vietnamese thousands separator, matching the numbers in the messages."""
    return f"{number:,}".replace(",", ".")


# ---------------------------------------------------------------- reading one file


@dataclass(frozen=True)
class ReadSettings:
    """How to read one workbook. Anything left as None is found automatically."""

    template_key: str | None = None
    sheet: str | None = None
    start_row: int | None = None
    mapping: Mapping[str, int] | None = None
    extra_columns: tuple[int, ...] = ()


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
    # Things worth knowing that do not keep the file out, e.g. one worker role unmapped.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ColumnOption:
    column: int
    letter: str
    header: str
    sample: str


@dataclass(frozen=True)
class FileReading:
    """Everything decided while reading one workbook, for a caller to show or change."""

    outcome: FileOutcome
    previews: tuple[SheetPreview, ...] = ()
    template: ReportTemplate | None = None
    template_suggested: bool = False
    sheet: SheetPreview | None = None
    sheet_found: bool = False
    start_row: int | None = None
    start_row_detected: bool = False
    layout: SheetLayout | None = None
    mapping: Mapping[str, int] = field(default_factory=dict)
    extras: Mapping[str, int] = field(default_factory=dict)


def template_by_key(key: str) -> ReportTemplate | None:
    return next((template for template in TEMPLATES if template.key == key), None)


def sample_value(preview: SheetPreview, column: int, start_row: int) -> str:
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


def column_options(
    preview: SheetPreview, layout: SheetLayout, start_row: int
) -> list[ColumnOption]:
    """Columns worth offering: those with a header, a value, or a match."""
    width = max((len(row) for row in preview.rows), default=0)
    matched = {match.column for match in layout.matches.values()}
    options = []
    for column in range(width):
        header = layout.headers.get(column, "")
        sample = sample_value(preview, column, start_row)
        if header or sample or column in matched:
            options.append(ColumnOption(column, column_letter(column), header, sample))
    return options


def extra_labels(columns: Iterable[int], layout: SheetLayout) -> dict[str, int]:
    """Labels for columns read only to be shown, e.g. "K — Ghi chú"."""
    return {
        f"{column_letter(column)} — {layout.headers.get(column, '')}".strip(
            " —"
        ): column
        for column in columns
    }


def default_mapping(template: ReportTemplate, layout: SheetLayout) -> dict[str, int]:
    """The columns matched automatically; ambiguous and missing fields stay unmapped."""
    return {
        spec.key: match.column
        for spec in template.fields
        if (match := layout.matches[spec.key]).column is not None
    }


def needs_attention(template: ReportTemplate, layout: SheetLayout) -> bool:
    return any(
        spec.required and layout.matches[spec.key].status is not MatchStatus.AUTO
        for spec in template.fields
    )


def credit_result(
    result: ImportResult,
    template: ReportTemplate,
    mapping: Mapping[str, int],
    file_name: str,
    sheet: str,
) -> FileOutcome:
    """Credit the rows to employees, or say why the file cannot be credited."""
    slots = template.worker_slots
    unmapped = [slot.role for slot in slots if not is_slot_mapped(slot, dict(mapping))]
    if len(unmapped) == len(slots):
        return FileOutcome(
            file_name,
            result,
            excluded_because=NO_EMPLOYEE_COLUMNS,
            template_label=template.label,
            sheet=sheet,
        )
    warnings = (
        ("Chưa đủ cột nên không tính phần của: " + ", ".join(unmapped),)
        if unmapped
        else ()
    )
    return FileOutcome(
        file_name,
        result,
        credit_workers(result, template, dict(mapping), file_name),
        template_label=template.label,
        sheet=sheet,
        warnings=warnings,
    )


def read_settings_signature(
    sheet: str,
    template: ReportTemplate,
    start_row: int,
    mapping: Mapping[str, int],
    extras: Mapping[str, int],
) -> tuple[Any, ...]:
    return (
        sheet,
        template.key,
        start_row,
        tuple(sorted(mapping.items())),
        tuple(sorted(extras.items())),
    )


def read_workbook(
    file_name: str,
    data: bytes,
    digest: str,
    settings: ReadSettings,
    reader: CachedReader,
) -> FileReading:
    """Read one workbook, finding automatically whatever the settings leave open.

    Raises nothing for a bad file: the outcome says why it was left out.
    """
    try:
        previews: list[SheetPreview] = reader(
            cache_key("previews", digest),
            f"Đang đọc danh sách sheet của {file_name}...",
            lambda: read_previews(data),
        )
    except ExcelImportError as error:
        return FileReading(FileOutcome(file_name, excluded_because=str(error)))

    suggested_template = suggest_template(file_name, TEMPLATES)
    template = (
        template_by_key(settings.template_key)
        if settings.template_key
        else suggested_template
    )
    if template is None:
        return FileReading(
            FileOutcome(file_name, excluded_because="chưa chọn loại báo cáo"),
            tuple(previews),
        )
    suggested_sheet = suggest_sheet(previews, template)
    by_name = {preview.name: preview for preview in previews}
    sheet_name = settings.sheet or suggested_sheet
    preview = by_name.get(sheet_name) if sheet_name else None
    base = FileReading(
        FileOutcome(
            file_name,
            excluded_because="chưa chọn sheet",
            template_label=template.label,
        ),
        tuple(previews),
        template,
        template is suggested_template,
        None,
        suggested_sheet is not None,
    )
    if preview is None:
        return base

    detected = analyze_sheet(preview, template).data_start_row
    start_row = settings.start_row or detected or HEADER_SCAN_ROWS
    start_row = min(max(start_row, 2), PREVIEW_ROWS)
    layout = analyze_sheet(preview, template, start_row)
    mapping = (
        dict(settings.mapping)
        if settings.mapping is not None
        else default_mapping(template, layout)
    )
    extras = extra_labels(settings.extra_columns, layout)
    signature = read_settings_signature(
        preview.name, template, start_row, mapping, extras
    )
    reading = replace(
        base,
        sheet=preview,
        start_row=start_row,
        start_row_detected=detected is not None,
        layout=layout,
        mapping=mapping,
        extras=extras,
    )
    try:
        result: ImportResult = reader(
            cache_key("rows", digest, signature),
            f"Đang đọc dữ liệu {file_name}...",
            lambda: read_rows(
                data, preview.name, template, dict(mapping), start_row, dict(extras)
            ),
        )
    except ExcelImportError as error:
        outcome = FileOutcome(
            file_name,
            excluded_because=str(error),
            template_label=template.label,
            sheet=preview.name,
        )
    else:
        outcome = replace(
            credit_result(result, template, mapping, file_name, preview.name),
            template_key=template.key,
            file_digest=digest,
            read_settings=repr(signature),
        )
    return replace(reading, outcome=outcome)


def preview_rows(
    result: ImportResult,
    template: ReportTemplate,
    extra_labels: Sequence[str],
    limit: int,
) -> list[dict[str, Any]]:
    """The first rows read, keyed by the labels shown to people."""
    table = []
    for row in result.rows[:limit]:
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


# ---------------------------------------------------------------- several files


@dataclass(frozen=True)
class UploadCandidate:
    name: str
    size: int
    digest: str


@dataclass(frozen=True)
class SkippedFile:
    name: str
    reason: str


def deduplicate[T: UploadCandidate](
    files: Sequence[T],
) -> tuple[list[T], list[SkippedFile]]:
    """The same workbook uploaded twice would double everyone's output."""
    kept: dict[str, T] = {}
    skipped = []
    limit = MAX_FILE_BYTES // (1024 * 1024)
    for file in files:
        if file.size > MAX_FILE_BYTES:
            skipped.append(SkippedFile(file.name, f"file vượt quá {limit} MB"))
        elif file.digest in kept:
            skipped.append(
                SkippedFile(file.name, f"trùng nội dung với {kept[file.digest].name}")
            )
        else:
            kept[file.digest] = file
    return list(kept.values()), skipped


class FileStatus(StrEnum):
    READY = "ready"
    # Read, but with rejected rows or totals that disagree with the file.
    CHECK = "check"
    EXCLUDED = "excluded"


def file_status(outcome: FileOutcome) -> tuple[FileStatus, str]:
    """A status and the sentence explaining it."""
    if outcome.excluded_because:
        return FileStatus.EXCLUDED, f"Chưa tính: {outcome.excluded_because.rstrip('.')}"
    result = outcome.result
    if result is None:
        return FileStatus.EXCLUDED, "Chưa đọc được"
    notes = []
    if result.rejected:
        notes.append(f"{count_text(len(result.rejected))} dòng lỗi")
    if any(total.matches_stated is False for total in result.totals):
        notes.append("lệch tổng trong file")
    if notes:
        return FileStatus.CHECK, ", ".join(notes)
    return FileStatus.READY, "Sẵn sàng"


@dataclass(frozen=True)
class CombinedOutput:
    outputs: list[WorkerOutput]
    problems: list[CreditProblem]
    company_shares: list[CompanyShare]


def combine_outputs(
    outcomes: Iterable[FileOutcome], rosters: Mapping[str, Roster | None]
) -> CombinedOutput:
    """Every file's credited output, with full names from the timesheets."""
    credited = [outcome.credited for outcome in outcomes if outcome.credited]
    outputs = fill_full_names(
        (output for item in credited for output in item.outputs),
        {name: roster for name, roster in rosters.items() if roster},
    )
    return CombinedOutput(
        outputs,
        [problem for item in credited for problem in item.problems],
        [share for item in credited for share in item.company_shares],
    )


@dataclass(frozen=True)
class RecordedChanges:
    changed_rows: int
    edited_files: list[str]
    # Versions recorded now, for the caller to skip next time.
    versions: list[tuple[str, str, str]]


def record_changes(
    db_path: Path,
    outcomes: Iterable[FileOutcome],
    outputs: Iterable[WorkerOutput],
    already_recorded: set[tuple[str, str, str]],
) -> RecordedChanges:
    """Compare each read file with its last stored version. Raises ChangeStorageError."""
    by_source: defaultdict[str, list[WorkerOutput]] = defaultdict(list)
    for output in outputs:
        by_source[output.source].append(output)
    found = 0
    edited: list[str] = []
    versions: list[tuple[str, str, str]] = []
    for outcome in outcomes:
        if outcome.credited is None or not outcome.file_digest:
            continue
        key = source_key(outcome.template_key, outcome.file_name)
        version = (key, outcome.file_digest, outcome.read_settings)
        if version in already_recorded:
            continue
        recorded = record_version(
            db_path,
            TrackedFile(
                key,
                outcome.file_name,
                outcome.file_digest,
                by_source[outcome.file_name],
            ),
        )
        versions.append(version)
        if recorded.outcome is VersionOutcome.CHANGED and recorded.changes:
            found += recorded.changes
            edited.append(outcome.file_name)
    return RecordedChanges(found, edited, versions)


# ---------------------------------------------------------------- problems to check


class Severity(StrEnum):
    # Output would be wrong or reach nobody.
    ERROR = "error"
    # Output may be wrong; worth a look before sending.
    WARNING = "warning"
    # Nothing is wrong; shown so the operator knows.
    INFO = "info"


@dataclass(frozen=True)
class Issue:
    severity: Severity
    title: str
    detail: str
    rows: tuple[dict[str, Any], ...] = ()


def incomplete_identities_issue(people: list[IncompleteIdentity]) -> Issue | None:
    if not people:
        return None
    without_code = sum(person.missing_code for person in people)
    title = f"{count_text(len(people))} người chưa có đủ họ tên hoặc mã số"
    if without_code:
        title += (
            f" ({count_text(without_code)} người thiếu mã, không nhận được sản lượng)"
        )
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


def roster_issues(rosters: Mapping[str, Roster | None]) -> list[Issue]:
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
                f"{count_text(len(missing))} file không có sheet {sheets} để tra họ tên",
                ", ".join(missing),
            )
        )
    return issues


def file_issues(outcomes: Iterable[FileOutcome]) -> list[Issue]:
    issues = []
    for outcome in outcomes:
        if outcome.excluded_because:
            reason = outcome.excluded_because.rstrip(".")
            issues.append(
                Issue(
                    Severity.ERROR,
                    f"Chưa tính file {outcome.file_name}",
                    f"Lý do: {reason}. Hãy chỉnh cách đọc file này.",
                )
            )
            continue
        if outcome.result is None:
            continue
        if outcome.result.rejected:
            issues.append(
                Issue(
                    Severity.ERROR,
                    f"{outcome.file_name}: {count_text(len(outcome.result.rejected))} "
                    "dòng lỗi chưa được tính",
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
                    "Xem chi tiết trong phần đọc của file này.",
                )
            )
    return issues


def day_issues(
    outputs: list[WorkerOutput], problems: Iterable[CreditProblem], day: date
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
                f"{count_text(len(day_problems))} phần sản lượng ngày này chưa gán được "
                "cho nhân viên",
                "Các phần này không có trong danh sách gửi.",
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
                f"{count_text(len(conflicts))} mã số gắn với nhiều họ tên khác nhau",
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


def company_share_issue(shares: Iterable[CompanyShare], day: date) -> Issue | None:
    day_shares = [share for share in shares if share.day == day]
    if not day_shares:
        return None
    return Issue(
        Severity.INFO,
        f"{count_text(len(day_shares))} phần ghi cho công ty, không gửi cho ai",
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


def general_issues(
    outcomes: Sequence[FileOutcome],
    rosters: Mapping[str, Roster | None],
    combined: CombinedOutput,
) -> list[Issue]:
    """Problems that do not depend on the day chosen."""
    issues = [*file_issues(outcomes), *roster_issues(rosters)]
    if incomplete := incomplete_identities_issue(
        find_incomplete_identities(combined.outputs, combined.problems)
    ):
        issues.append(incomplete)
    return issues


def issues_for_day(
    outcomes: Sequence[FileOutcome],
    rosters: Mapping[str, Roster | None],
    combined: CombinedOutput,
    day: date,
) -> list[Issue]:
    """Everything to check before sending a day's output, most serious first."""
    issues = [
        *general_issues(outcomes, rosters, combined),
        *day_issues(combined.outputs, combined.problems, day),
    ]
    if company := company_share_issue(combined.company_shares, day):
        issues.append(company)
    order = list(Severity)
    return sorted(issues, key=lambda issue: order.index(issue.severity))
