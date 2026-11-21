"""Read production figures from Excel by header text, refusing to guess when unsure."""

import math
import re
import unicodedata
import warnings
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from io import BytesIO
from typing import Annotated, Any
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.workbook.workbook import Workbook
from pydantic import Field, TypeAdapter, ValidationError

from gui_san_luong.domain.production_templates import (
    FieldKind,
    FieldSpec,
    ReportTemplate,
)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_SHEET_ROWS = 200_000
HEADER_SCAN_ROWS = 15
PREVIEW_ROWS = 80
TOTAL_TOLERANCE = 0.01
TOTAL_ROW_PREFIXES = ("tong", "cong", "total", "subtotal")
# A cell whose formula is followed by no result, or an empty one, before the cell closes.
# Excel always writes a cell's formula first, then its result.
UNCACHED_FORMULA_CELL = re.compile(
    rb"<(?:\w+:)?c\b([^>]*)>\s*"
    rb"<(?:\w+:)?f\b([^>]*?)(?:/>|>[^<]*</(?:\w+:)?f>)\s*"
    rb"(?:<(?:\w+:)?v\b[^>]*/>|<(?:\w+:)?v\b[^>]*>\s*</(?:\w+:)?v>)?\s*"
    rb"</(?:\w+:)?c>"
)
CELL_REFERENCE = re.compile(rb'\br="([A-Za-z]+)(\d+)"')
FORMULA_TYPE_ARRAY = re.compile(rb'\bt="array"')
# Above this, the sheet XML is scanned as a stream instead of being held in memory.
MAX_FAST_SCAN_BYTES = 400 * 1024 * 1024
EXCEL_ERROR_PATTERN = re.compile(r"^#(REF!|N/A|VALUE!|DIV/0!|NAME\?|NUM!|NULL!)")

# openpyxl warns about Excel features it ignores, such as print headers; none affect cell values.
warnings.filterwarnings("ignore", category=UserWarning, module=r"openpyxl\..*")

_QUANTITY_ADAPTER: TypeAdapter[float] = TypeAdapter(
    Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
)
_DATE_ADAPTER: TypeAdapter[datetime] = TypeAdapter(
    Annotated[datetime, Field(strict=True)]
)


class ExcelImportError(Exception):
    """A user-facing reason why a workbook cannot be read."""


class MatchStatus(StrEnum):
    AUTO = "auto"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"


@dataclass(frozen=True)
class SheetPreview:
    name: str
    hidden: bool
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class FieldMatch:
    field_key: str
    status: MatchStatus
    column: int | None = None
    candidates: tuple[int, ...] = ()


@dataclass(frozen=True)
class SheetLayout:
    data_start_row: int | None
    headers: dict[int, str]
    matches: dict[str, FieldMatch]

    def matched_required(self, template: ReportTemplate) -> int:
        return sum(
            1
            for spec in template.fields
            if spec.required and self.matches[spec.key].status is MatchStatus.AUTO
        )


@dataclass(frozen=True)
class RejectedRow:
    row: int
    reason: str


@dataclass(frozen=True)
class FieldTotal:
    field_key: str
    label: str
    all_rows: float
    counted: float
    stated: float | None

    @property
    def matches_stated(self) -> bool | None:
        if self.stated is None:
            return None
        return math.isclose(self.counted, self.stated, abs_tol=TOTAL_TOLERANCE)


@dataclass
class ImportResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    blank_rows: int = 0
    total_rows: int = 0
    repeated_rows: int = 0
    totals: list[FieldTotal] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)


COUNTED_KEY = "_counted"
ROW_KEY = "_row"


def normalize_header(text: str) -> str:
    """Lowercase, remove Vietnamese accents and punctuation so headers compare reliably."""
    decomposed = unicodedata.normalize("NFD", text.replace("Đ", "D").replace("đ", "d"))
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_marks.lower()).split())


def column_letter(column: int) -> str:
    return get_column_letter(column + 1)


@contextmanager
def open_workbook(data: bytes, *, data_only: bool) -> Iterator[Workbook]:
    if len(data) > MAX_FILE_BYTES:
        raise ExcelImportError(f"File vượt quá {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=data_only)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, ParseError) as error:
        raise ExcelImportError(
            "Không đọc được file. Hãy lưu lại dạng .xlsx rồi thử lại."
        ) from error
    try:
        yield workbook
    finally:
        workbook.close()


def read_previews(data: bytes) -> list[SheetPreview]:
    """Read the first rows of every sheet, enough to recognize headers and column types."""
    with open_workbook(data, data_only=True) as workbook:
        return [
            SheetPreview(
                name=sheet.title,
                hidden=sheet.sheet_state != "visible",
                rows=tuple(sheet.iter_rows(max_row=PREVIEW_ROWS, values_only=True)),
            )
            for sheet in workbook.worksheets
        ]


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _header_cells(
    rows: Sequence[Sequence[Any]], data_start_row: int
) -> dict[int, list[str]]:
    """Collect header text per column, since reports split one header over several rows."""
    cells: defaultdict[int, list[str]] = defaultdict(list)
    for row in rows[: data_start_row - 1]:
        for column, value in enumerate(row):
            if isinstance(value, str) and value.strip():
                cells[column].append(" ".join(value.split()))
    return dict(cells)


def _coverage(text: str, aliases: Iterable[str]) -> float:
    normalized = normalize_header(text)
    tokens = normalized.split()
    return max(
        (
            len(alias.split()) / len(tokens)
            for alias in aliases
            if f" {alias} " in f" {normalized} "
        ),
        default=0.0,
    )


def _header_score(cells: Sequence[str], aliases: Iterable[str]) -> tuple[float, float]:
    """Rank a column by how much of its header an alias explains.

    The best single cell comes first so a sheet title above "NGÀY" does not lose to
    "Ngày giao hàng". The joined header breaks ties so "S.LƯỢNG" beats "S.LƯỢNG / ĐƠN VỊ (cái)".
    """
    aliases = tuple(aliases)
    joined = " ".join(cells)
    best_cell = max(
        (_coverage(text, aliases) for text in (*cells, joined)), default=0.0
    )
    return best_cell, _coverage(joined, aliases)


def _find_data_start(rows: Sequence[Sequence[Any]], date_spec: FieldSpec) -> int | None:
    """The first row holding a real date under a date-like header starts the data."""
    for row_number, row in enumerate(rows[:HEADER_SCAN_ROWS], start=1):
        headers = _header_cells(rows, row_number)
        for column, value in enumerate(row):
            if isinstance(value, datetime) and any(
                _header_score(headers.get(column, ()), date_spec.aliases)
            ):
                return row_number
    return None


def _column_fits_kind(
    rows: Sequence[Sequence[Any]], column: int, kind: FieldKind
) -> bool:
    """Tell columns with the same header apart, like worker names and their pay ratios."""
    if kind is FieldKind.CODE:
        return True
    values = [
        row[column] for row in rows if column < len(row) and not _is_blank(row[column])
    ]
    if not values:
        return True
    expected: type | tuple[type, ...]
    if kind is FieldKind.DATE:
        expected = datetime
    elif kind is FieldKind.NUMBER:
        expected = (int, float)
    else:
        expected = str
    return sum(isinstance(value, expected) for value in values) * 2 >= len(values)


def _best_columns(
    headers: dict[int, list[str]], aliases: Iterable[str]
) -> tuple[int, ...]:
    """The columns whose headers fit the aliases best; more than one means it is a tie."""
    aliases = tuple(aliases)
    scores = {
        column: score
        for column, cells in headers.items()
        if any(score := _header_score(cells, aliases))
    }
    if not scores:
        return ()
    best = max(scores.values())
    return tuple(sorted(column for column, score in scores.items() if score == best))


def _match_anchored(
    spec: FieldSpec, headers: dict[int, list[str]], data_rows: Sequence[Sequence[Any]]
) -> FieldMatch:
    anchors = _best_columns(headers, spec.anchor_aliases)
    if not anchors:
        return FieldMatch(spec.key, MatchStatus.MISSING)
    columns = tuple(anchor + spec.anchor_offset for anchor in anchors)
    if len(columns) > 1:
        return FieldMatch(spec.key, MatchStatus.AMBIGUOUS, candidates=columns)
    if not _column_fits_kind(data_rows, columns[0], spec.kind):
        return FieldMatch(spec.key, MatchStatus.MISSING)
    return FieldMatch(spec.key, MatchStatus.AUTO, column=columns[0], candidates=columns)


def _match_field(
    spec: FieldSpec, headers: dict[int, list[str]], data_rows: Sequence[Sequence[Any]]
) -> FieldMatch:
    if spec.anchor_aliases:
        return _match_anchored(spec, headers, data_rows)
    scores = {
        column: score
        for column, cells in headers.items()
        if any(score := _header_score(cells, spec.aliases))
        and _column_fits_kind(data_rows, column, spec.kind)
    }
    if not scores:
        return FieldMatch(spec.key, MatchStatus.MISSING)
    best = max(scores.values())
    leaders = tuple(sorted(column for column, score in scores.items() if score == best))
    if len(leaders) > 1:
        return FieldMatch(spec.key, MatchStatus.AMBIGUOUS, candidates=leaders)
    return FieldMatch(spec.key, MatchStatus.AUTO, column=leaders[0], candidates=leaders)


def analyze_sheet(
    preview: SheetPreview, template: ReportTemplate, data_start_row: int | None = None
) -> SheetLayout:
    """Match template fields to columns. A field stays unmapped unless one column clearly wins."""
    start = data_start_row or _find_data_start(
        preview.rows, template.field(template.date_field)
    )
    if start is None:
        return SheetLayout(
            None,
            {},
            {
                spec.key: FieldMatch(spec.key, MatchStatus.MISSING)
                for spec in template.fields
            },
        )
    headers = _header_cells(preview.rows, start)
    data_rows = preview.rows[start - 1 :]
    matches = {
        spec.key: _match_field(spec, headers, data_rows) for spec in template.fields
    }
    joined = {column: " ".join(cells) for column, cells in headers.items()}
    return SheetLayout(start, joined, matches)


def suggest_sheet(
    previews: Sequence[SheetPreview], template: ReportTemplate
) -> str | None:
    """Pick the one visible sheet with all required fields, or None when that would be a guess.

    Hidden sheets are skipped because they are often stale copies. Workbooks may hold several
    similar sheets with different figures, so a known name settles a tie and nothing else does.
    """
    required = sum(spec.required for spec in template.fields)
    complete = [
        preview.name
        for preview in previews
        if not preview.hidden
        and analyze_sheet(preview, template).matched_required(template) == required
    ]
    if len(complete) == 1:
        return complete[0]
    for hint in template.sheet_hints:
        for name in complete:
            if normalize_header(name) == hint:
                return name
    return None


def suggest_template(
    file_name: str, templates: Sequence[ReportTemplate]
) -> ReportTemplate | None:
    tokens = normalize_header(file_name).split()
    found = [
        t
        for t in templates
        if any(k in tokens for k in t.file_keywords)
        and not any(k in tokens for k in t.file_excludes)
    ]
    return found[0] if len(found) == 1 else None


def _check_value(spec: FieldSpec, value: object) -> tuple[object, str | None]:
    if _is_blank(value):
        return None, f"Thiếu {spec.label}" if spec.required else None
    if isinstance(value, str) and EXCEL_ERROR_PATTERN.match(value.strip()):
        return None, f"{spec.label} có giá trị lỗi {value.strip()}"
    try:
        if spec.kind is FieldKind.DATE:
            return _DATE_ADAPTER.validate_python(value).date(), None
        if spec.kind is FieldKind.NUMBER:
            return _QUANTITY_ADAPTER.validate_python(value), None
    except ValidationError:
        expected = "ngày" if spec.kind is FieldKind.DATE else "số không âm"
        return None, f"{spec.label} không phải {expected}: {str(value)[:30]!r}"
    return (value.strip() if isinstance(value, str) else value), None


def _cell(row: Sequence[Any], column: int | None) -> Any:
    return row[column] if column is not None and column < len(row) else None


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _column_index(reference: str) -> int:
    """Zero-based column of a cell reference such as "AB12"."""
    letters = reference.rstrip("0123456789")
    index = 0
    for letter in letters.upper():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def _formula_cells(
    archive: zipfile.ZipFile, sheet_path: str, columns: set[int], min_row: int
) -> set[tuple[int, int]]:
    """Cells in these columns whose formula may lack a stored result.

    As (Excel row, zero-based column); the caller still checks that the read value is empty.
    Read straight from the sheet XML rather than through openpyxl's formula mode, which
    rewrites every shared formula for every cell and was most of an import's time. Only
    whether a formula is present matters, not its text. Array formulas are left out, as
    openpyxl never reported them as formula text either.
    """
    if archive.getinfo(sheet_path).file_size <= MAX_FAST_SCAN_BYTES:
        found = _scan_formulas_fast(archive.read(sheet_path), columns, min_row)
        if found is not None:
            return found
    return _scan_formulas_exact(archive, sheet_path, columns, min_row)


def _scan_formulas_fast(
    xml: bytes, columns: set[int], min_row: int
) -> set[tuple[int, int]] | None:
    """Formula cells with no stored result, or None when a cell lacks its reference."""
    found: set[tuple[int, int]] = set()
    for match in UNCACHED_FORMULA_CELL.finditer(xml):
        cell_attributes, formula_attributes = match.groups()
        if FORMULA_TYPE_ARRAY.search(formula_attributes):
            continue
        reference = CELL_REFERENCE.search(cell_attributes)
        if reference is None:
            return None
        row = int(reference.group(2))
        column = _column_index(reference.group(1).decode())
        if row >= min_row and column in columns:
            found.add((row, column))
    return found


def _scan_formulas_exact(
    archive: zipfile.ZipFile, sheet_path: str, columns: set[int], min_row: int
) -> set[tuple[int, int]]:
    """Every formula cell, following the XML structure; slower but needs no references."""
    found: set[tuple[int, int]] = set()
    row_number = 0
    column = -1
    with archive.open(sheet_path) as stream:
        for event, element in ElementTree.iterparse(stream, events=("start", "end")):
            name = _local_name(element.tag)
            if event == "start":
                if name == "row":
                    reference = element.get("r")
                    row_number = int(reference) if reference else row_number + 1
                    column = -1
                continue
            if name == "c":
                reference = element.get("r")
                column = _column_index(reference) if reference else column + 1
                if row_number >= min_row and column in columns:
                    formula = next(
                        (child for child in element if _local_name(child.tag) == "f"),
                        None,
                    )
                    if formula is not None and formula.get("t") != "array":
                        found.add((row_number, column))
            elif name == "row":
                # Rows are not needed once read; clearing keeps memory flat on big sheets.
                element.clear()
    return found


def _stated_total(
    rows: Sequence[Sequence[Any]], column: int | None, counted: float
) -> float | None:
    """Find the control total kept above the header.

    The same column may also hold a helper number such as a pay ratio, so a cell agreeing with
    the counted total is preferred; otherwise the topmost number is shown as the mismatch.
    """
    numbers = [
        float(value)
        for row in rows
        if isinstance(value := _cell(row, column), int | float)
        and not isinstance(value, bool)
    ]
    for number in numbers:
        if math.isclose(number, counted, abs_tol=TOTAL_TOLERANCE):
            return number
    return numbers[0] if numbers else None


def _control_column(
    template: ReportTemplate,
    spec: FieldSpec,
    headers: dict[int, list[str]],
    mapped: int,
) -> int | None:
    aliases = template.control_aliases.get(spec.key)
    if aliases is None:
        return mapped
    leaders = _best_columns(headers, aliases)
    return leaders[0] if len(leaders) == 1 else None


def _build_totals(
    template: ReportTemplate,
    mapping: dict[str, int],
    rows: list[dict[str, Any]],
    header_rows: Sequence[Sequence[Any]],
    headers: dict[int, list[str]],
) -> list[FieldTotal]:
    totals = []
    shares = {
        slot.quantity_field
        for slot in template.worker_slots
        if slot.quantity_field != template.quantity_field
    }
    ratios = {slot.ratio_field for slot in template.worker_slots}
    for spec in template.fields:
        if (
            spec.kind is not FieldKind.NUMBER
            or spec.key not in mapping
            or spec.key in ratios
        ):
            continue
        # A repeated row is one output but still a separate person's share of it.
        values = [
            (row.get(spec.key) or 0.0, row[COUNTED_KEY] or spec.key in shares)
            for row in rows
        ]
        control = _control_column(template, spec, headers, mapping[spec.key])
        counted_total = math.fsum(value for value, counted in values if counted)
        totals.append(
            FieldTotal(
                field_key=spec.key,
                label=spec.label,
                all_rows=math.fsum(value for value, _ in values),
                counted=counted_total,
                stated=_stated_total(header_rows, control, counted_total),
            )
        )
    return totals


def _build_groups(
    template: ReportTemplate, mapping: dict[str, int], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    keys = [key for key in template.group_by if key in mapping]
    summed: defaultdict[tuple[Any, ...], float] = defaultdict(float)
    for row in rows:
        if row[COUNTED_KEY]:
            group = tuple(row.get(key) for key in keys)
            summed[group] += row.get(template.quantity_field) or 0.0
    quantity_label = template.field(template.quantity_field).label
    return [
        {
            **{
                template.field(key).label: value
                for key, value in zip(keys, group, strict=True)
            },
            quantity_label: total,
        }
        for group, total in sorted(
            summed.items(), key=lambda item: tuple(map(str, item[0]))
        )
    ]


def _parse_row(
    template: ReportTemplate,
    mapping: dict[str, int],
    extras: dict[str, int],
    row_number: int,
    value_row: Sequence[Any],
) -> tuple[dict[str, Any] | None, str | None]:
    parsed: dict[str, Any] = {ROW_KEY: row_number}
    for spec in template.fields:
        if spec.key not in mapping:
            continue
        value, problem = _check_value(spec, _cell(value_row, mapping[spec.key]))
        # A broken optional code (#N/A) leaves the output valid; it is reported against the
        # person when output is credited, rather than discarding everyone else's share.
        if problem and (spec.required or spec.kind is not FieldKind.CODE):
            return None, problem
        parsed[spec.key] = value
    for label, column in extras.items():
        parsed[label] = _cell(value_row, column)
    return parsed, None


def read_rows(
    data: bytes,
    sheet_name: str,
    template: ReportTemplate,
    mapping: dict[str, int],
    data_start_row: int,
    extras: dict[str, int] | None = None,
) -> ImportResult:
    """Read every data row. Bad rows are reported, never dropped silently or treated as zero."""
    missing = [
        spec.label
        for spec in template.fields
        if spec.required and spec.key not in mapping
    ]
    if missing:
        raise ExcelImportError("Chưa chọn cột bắt buộc: " + ", ".join(missing))

    result = ImportResult()
    date_column = mapping[template.date_field]
    quantity_column = mapping[template.quantity_field]
    seen_keys: Counter[tuple[Any, ...]] = Counter()

    with open_workbook(data, data_only=True) as values:
        if sheet_name not in values.sheetnames:
            raise ExcelImportError(f"Không có sheet {sheet_name!r} trong file.")
        value_sheet = values[sheet_name]
        if (value_sheet.max_row or 0) > MAX_SHEET_ROWS:
            raise ExcelImportError(f"Sheet có hơn {MAX_SHEET_ROWS:,} dòng.")
        header_rows = list(
            value_sheet.iter_rows(max_row=data_start_row - 1, values_only=True)
        )
        headers = _header_cells(header_rows, data_start_row)
        # openpyxl keeps where the sheet lives in the package only on this private attribute.
        sheet_path = getattr(value_sheet, "_worksheet_path", None)
        if not isinstance(sheet_path, str):
            raise ExcelImportError(f"Không đọc được sheet {sheet_name!r}.")
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                formula_cells = _formula_cells(
                    archive, sheet_path, {date_column, quantity_column}, data_start_row
                )
        except (KeyError, ParseError, zipfile.BadZipFile) as error:
            raise ExcelImportError(
                "Không đọc được file. Hãy lưu lại dạng .xlsx rồi thử lại."
            ) from error

        for row_number, value_row in enumerate(
            value_sheet.iter_rows(min_row=data_start_row, values_only=True),
            start=data_start_row,
        ):
            date_value = _cell(value_row, date_column)
            quantity_value = _cell(value_row, quantity_column)
            # A formula Excel never calculated reads as empty; it must not count as no output.
            uncached = [
                column
                for column in (date_column, quantity_column)
                if (row_number, column) in formula_cells
                and _cell(value_row, column) is None
            ]
            if uncached:
                letters = ", ".join(column_letter(column) for column in uncached)
                result.rejected.append(
                    RejectedRow(
                        row_number,
                        f"Công thức ở cột {letters} chưa có giá trị; hãy mở file và lưu lại",
                    )
                )
                continue
            if isinstance(date_value, str) and normalize_header(date_value).startswith(
                TOTAL_ROW_PREFIXES
            ):
                result.total_rows += 1
                continue
            # Formulas pre-filled down the sheet give 0 under no date, and signature lines sit
            # below the data. Without a date and a quantity they hold no output to lose.
            if not isinstance(date_value, datetime) and (
                _is_blank(quantity_value) or quantity_value == 0
            ):
                result.blank_rows += 1
                continue

            parsed, problem = _parse_row(
                template, mapping, extras or {}, row_number, value_row
            )
            if parsed is None:
                result.rejected.append(
                    RejectedRow(row_number, problem or "Dòng không hợp lệ")
                )
                continue

            key = tuple(parsed.get(name) for name in template.duplicate_key)
            parsed[COUNTED_KEY] = not template.duplicate_key or key not in seen_keys
            seen_keys[key] += 1
            result.repeated_rows += not parsed[COUNTED_KEY]
            result.rows.append(parsed)

    result.totals = _build_totals(template, mapping, result.rows, header_rows, headers)
    result.groups = _build_groups(template, mapping, result.rows)
    return result
