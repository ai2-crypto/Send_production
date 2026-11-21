"""Credit each employee with their share of output, across every imported report.

People move between departments within a month, so one employee may appear in several files.
The employee code links them: nicknames repeat across departments and cannot identify anyone.
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from gui_san_luong.domain.production_templates import (
    DetailFields,
    ReportTemplate,
    WorkerSlot,
)
from gui_san_luong.services.excel_import import ROW_KEY, ImportResult, normalize_header

BLANK_CODES = ("", "0")
MIN_FULL_NAME_WORDS = 2
# Written instead of a person when output belongs to the company, so nobody is sent it.
COMPANY_MARKER = "cty"
# Zalo refuses a consultation text above this length.
MAX_MESSAGE_CHARS = 2000
# Some item cells hold a long description; a line must stay far below the message limit.
MAX_ITEM_CHARS = 80
RATIO_DECIMALS = 3
SEPARATOR = "─" * 10
# No blank line between blocks: the "1)", "2)" numbering already marks where a row starts,
# and users asked for a compact message.
BLOCK_GAP = "\n"
# With no blank lines, indenting a row's later lines shows where each numbered row ends.
DETAIL_INDENT = "   "


class TextStyle(StrEnum):
    PLAIN = "plain"
    # Same layout, with an emoji before each kind of figure: Zalo text has no bold or colour,
    # and emoji are the only colour a reader's eye can catch.
    EMOJI = "emoji"


@dataclass(frozen=True)
class Markers:
    title: str = ""
    person: str = ""
    total: str = ""
    split: str = ""
    department: str = ""
    shift: str = ""
    machine: str = ""
    item: str = ""
    step: str = ""
    result: str = ""
    # Shift and machine on the numbered line, the item code below it where it is easy to read.
    item_on_own_line: bool = False


MARKERS = {
    TextStyle.PLAIN: Markers(),
    TextStyle.EMOJI: Markers(
        title="📅 ",
        person="👤 ",
        total="✅ ",
        split="⏱ ",
        department="🏭 ",
        shift="🕐 ",
        machine="⚙️ ",
        item="📦 ",
        step="🔧 ",
        result="👉 ",
        item_on_own_line=True,
    ),
}


def message_length(text: str) -> int:
    """Length as Zalo likely counts it, in UTF-16 units: most emoji count twice."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class OutputDetail:
    item: str | None = None
    machine: str | None = None
    shift: str | None = None
    overtime: bool = False
    step: str | None = None
    # Of the row's whole output, before it is split between people.
    overtime_quantity: float = 0.0


@dataclass(frozen=True)
class WorkerOutput:
    employee_code: str
    employee_name: str
    source: str
    department: str
    role: str
    process: str | None
    day: date
    quantity: float
    unit: str | None
    # The output of the whole row, before it is split between people.
    base_quantity: float
    detail: OutputDetail = OutputDetail()
    # Where the figure was read, so an edit can be shown row by row as it was made in Excel.
    excel_row: int | None = None


@dataclass(frozen=True)
class CreditProblem:
    source: str
    excel_row: int
    day: date
    reason: str
    department: str = ""
    # As written in the file, so a person without a code can still be found and fixed.
    employee_name: str = ""
    missing_code: bool = False


@dataclass(frozen=True)
class IncompleteIdentity:
    """A person whose code or full name is missing, so their output cannot be sent reliably."""

    employee_code: str | None
    employee_name: str
    departments: tuple[str, ...]
    sources: tuple[str, ...]
    excel_rows: int
    missing_code: bool
    missing_full_name: bool


@dataclass(frozen=True)
class CompanyShare:
    source: str
    excel_row: int
    day: date
    department: str
    quantity: float
    unit: str | None


@dataclass(frozen=True)
class CreditedOutput:
    outputs: list[WorkerOutput]
    problems: list[CreditProblem]
    company_shares: list[CompanyShare] = field(default_factory=list)


@dataclass(frozen=True)
class EmployeeIdentity:
    employee_code: str
    employee_name: str
    departments: tuple[str, ...]


@dataclass(frozen=True)
class DetailLine:
    detail: OutputDetail
    quantity: float
    base_quantity: float

    @property
    def ratio(self) -> float:
        """Worked out from the result, as reports state coefficients on different scales."""
        return self.quantity / self.base_quantity if self.base_quantity else 0.0

    @property
    def overtime_share(self) -> float:
        """The person's share splits between regular and overtime as the row's output does."""
        if not self.base_quantity:
            return 0.0
        return self.quantity * self.detail.overtime_quantity / self.base_quantity


@dataclass(frozen=True)
class EmployeeLine:
    department: str
    role: str
    process: str | None
    quantity: float
    unit: str | None
    details: tuple[DetailLine, ...] = ()


@dataclass(frozen=True)
class EmployeeDay:
    employee_code: str
    employee_name: str
    day: date
    lines: tuple[EmployeeLine, ...]
    # Every department this employee produced in across the imported period.
    period_departments: tuple[str, ...]

    @property
    def was_transferred(self) -> bool:
        return len(self.period_departments) > 1


def normalize_code(value: object) -> str | None:
    """Excel stores the same code as 12, 12.0 or "12 "; a fractional number is not a code."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int | str):
        text = str(value).strip()
        return None if text in BLANK_CODES else text
    return None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _slot_name(slot: WorkerSlot, row: dict[str, Any]) -> str:
    return next((name for key in slot.name_fields if (name := _text(row.get(key)))), "")


def _slot_role(slot: WorkerSlot, row: dict[str, Any]) -> str:
    stated = _text(row.get(slot.role_field)) if slot.role_field else ""
    return " ".join(stated.split()).capitalize() or slot.role


def _joined(fields: Iterable[str], row: dict[str, Any]) -> str | None:
    parts = [part for key in fields if (part := normalize_code(row.get(key)))]
    return " ".join(parts) or None


def _first(fields: Iterable[str], row: dict[str, Any]) -> str | None:
    return next(
        (part for key in fields if (part := normalize_code(row.get(key)))), None
    )


def _build_detail(fields: DetailFields, row: dict[str, Any]) -> OutputDetail:
    return OutputDetail(
        item=_first(fields.item_fields, row),
        step=_first(fields.step_fields, row),
        machine=_joined(fields.machine_fields, row),
        shift=_joined(fields.shift_fields, row),
        overtime=bool(
            fields.overtime_field and normalize_code(row.get(fields.overtime_field))
        ),
        overtime_quantity=_number(row, fields.overtime_quantity_field),
    )


def _number(row: dict[str, Any], key: str | None) -> float:
    """A blank cell here means no overtime, not a missing figure: the total still stands."""
    value = row.get(key) if key else None
    return float(value) if isinstance(value, int | float) else 0.0


def is_slot_mapped(slot: WorkerSlot, mapping: dict[str, int]) -> bool:
    return (
        slot.code_field in mapping
        and slot.quantity_field in mapping
        and (slot.ratio_field is None or slot.ratio_field in mapping)
        and any(key in mapping for key in slot.name_fields)
    )


def _is_company(code: str | None, name: str) -> bool:
    """A real code wins over the name, as that person's share must still reach them."""
    return (code or name).casefold() == COMPANY_MARKER


def _credit_slot(
    slot: WorkerSlot,
    row: dict[str, Any],
    template: ReportTemplate,
    source: str,
    day: date,
) -> WorkerOutput | CreditProblem | CompanyShare | None:
    code = normalize_code(row.get(slot.code_field))
    name = _slot_name(slot, row)
    row_number: int = row[ROW_KEY]
    quantity = row.get(slot.quantity_field)
    ratio = row.get(slot.ratio_field) if slot.ratio_field else 1.0

    # A row naming no worker has nobody to send to, so it is not a problem to report.
    if quantity == 0 or ratio == 0 or (not code and not name):
        return None
    if _is_company(code, name):
        if quantity is None:
            return None
        return CompanyShare(
            source,
            row_number,
            day,
            template.department,
            quantity * (1.0 if ratio is None else ratio),
            template.unit,
        )
    if ratio is None:
        # Assuming a whole share here would credit a machine's output to each person in full.
        return CreditProblem(
            source, row_number, day, f"{slot.role} {name or code}: thiếu hệ số"
        )
    if quantity is None:
        return CreditProblem(
            source, row_number, day, f"{slot.role} {name or code}: thiếu số lượng"
        )
    if not code:
        return CreditProblem(
            source,
            row_number,
            day,
            f"{slot.role} {name}: thiếu hoặc sai mã số nhân viên",
            department=template.department,
            employee_name=name,
            missing_code=True,
        )
    process_field = template.process_field
    return WorkerOutput(
        employee_code=code,
        employee_name=name or code,
        source=source,
        department=template.department,
        role=_slot_role(slot, row),
        process=(_text(row.get(process_field)) or None) if process_field else None,
        day=day,
        quantity=quantity * ratio,
        unit=template.unit,
        base_quantity=row[template.quantity_field],
        detail=_build_detail(template.detail, row),
        excel_row=row_number,
    )


def credit_workers(
    result: ImportResult,
    template: ReportTemplate,
    mapping: dict[str, int],
    source: str,
) -> CreditedOutput:
    """Split read rows into employee shares. A named person who cannot be credited is reported."""
    slots = [slot for slot in template.worker_slots if is_slot_mapped(slot, mapping)]
    outputs: list[WorkerOutput] = []
    problems: list[CreditProblem] = []
    company_shares: list[CompanyShare] = []
    for row in result.rows:
        day: date = row[template.date_field]
        for slot in slots:
            credited = _credit_slot(slot, row, template, source, day)
            if isinstance(credited, WorkerOutput):
                outputs.append(credited)
            elif isinstance(credited, CompanyShare):
                company_shares.append(credited)
            elif credited is not None:
                problems.append(credited)
    return CreditedOutput(outputs, problems, company_shares)


def _best_name(names: Iterable[str]) -> str:
    """Full names are longer than the nicknames other reports use for the same person."""
    return max(names, key=lambda name: (len(name), name))


def available_days(outputs: Iterable[WorkerOutput]) -> list[date]:
    return sorted({output.day for output in outputs})


def _sorted_details(details: Iterable[DetailLine]) -> tuple[DetailLine, ...]:
    """One entry per Excel row, so people can compare the message with the file line by line.

    The sort is stable, so rows that look identical keep the order they have in the file.
    """
    return tuple(
        sorted(
            details,
            key=lambda line: (
                line.detail.shift or "",
                line.detail.machine or "",
                line.detail.item or "",
                line.detail.step or "",
            ),
        )
    )


def summarize_day(outputs: Sequence[WorkerOutput], day: date) -> list[EmployeeDay]:
    """One entry per employee with output that day, without adding across different units."""
    names: defaultdict[str, set[str]] = defaultdict(set)
    departments: defaultdict[str, set[str]] = defaultdict(set)
    details: defaultdict[
        str,
        defaultdict[tuple[str, str, str | None, str | None], list[DetailLine]],
    ] = defaultdict(lambda: defaultdict(list))
    for output in outputs:
        code = output.employee_code
        names[code].add(output.employee_name)
        departments[code].add(output.department)
        if output.day == day:
            key = (output.department, output.role, output.process, output.unit)
            details[code][key].append(
                DetailLine(output.detail, output.quantity, output.base_quantity)
            )

    return [
        EmployeeDay(
            employee_code=code,
            employee_name=_best_name(names[code]),
            day=day,
            lines=tuple(
                EmployeeLine(
                    department,
                    role,
                    process,
                    math.fsum(line.quantity for line in rows),
                    unit,
                    _sorted_details(rows),
                )
                for (department, role, process, unit), rows in sorted(
                    lines.items(), key=lambda item: tuple(map(str, item[0]))
                )
            ),
            period_departments=tuple(sorted(departments[code])),
        )
        for code, lines in sorted(details.items())
    ]


def list_employees(outputs: Iterable[WorkerOutput]) -> list[EmployeeIdentity]:
    names: defaultdict[str, set[str]] = defaultdict(set)
    departments: defaultdict[str, set[str]] = defaultdict(set)
    for output in outputs:
        names[output.employee_code].add(output.employee_name)
        departments[output.employee_code].add(output.department)
    return [
        EmployeeIdentity(
            code, _best_name(names[code]), tuple(sorted(departments[code]))
        )
        for code in sorted(names)
    ]


def find_day_overlaps(
    outputs: Iterable[WorkerOutput], day: date
) -> dict[str, list[str]]:
    """Departments with output that day from several files, which may be counted twice."""
    sources: defaultdict[str, set[str]] = defaultdict(set)
    for output in outputs:
        if output.day == day:
            sources[output.department].add(output.source)
    return {
        department: sorted(names)
        for department, names in sources.items()
        if len(names) > 1
    }


def is_full_name(name: str) -> bool:
    """Nicknames used on the shop floor are one word; a Vietnamese full name has several.

    Words are split on spaces only: "TUAN_ANH" is a single nickname, not two names.
    """
    words = [word for word in name.split() if normalize_header(word)]
    return len(words) >= MIN_FULL_NAME_WORDS


def find_incomplete_identities(
    outputs: Iterable[WorkerOutput], problems: Iterable[CreditProblem]
) -> list[IncompleteIdentity]:
    """People across all files lacking a code, or whose files never give their full name.

    A missing code comes first, as that output reaches nobody at all.
    """
    by_code: defaultdict[str, list[WorkerOutput]] = defaultdict(list)
    for output in outputs:
        by_code[output.employee_code].append(output)
    found = [
        IncompleteIdentity(
            employee_code=code,
            employee_name=name,
            departments=tuple(sorted({o.department for o in rows})),
            sources=tuple(sorted({o.source for o in rows})),
            excel_rows=len(rows),
            missing_code=False,
            missing_full_name=True,
        )
        for code, rows in by_code.items()
        if not is_full_name(name := _best_name(o.employee_name for o in rows))
    ]

    # Without a code, the written name in its department is all that tells people apart.
    without_code: defaultdict[tuple[str, str], list[CreditProblem]] = defaultdict(list)
    for problem in problems:
        if problem.missing_code:
            key = (problem.department, normalize_header(problem.employee_name))
            without_code[key].append(problem)
    found.extend(
        IncompleteIdentity(
            employee_code=None,
            employee_name=name,
            departments=(department,),
            sources=tuple(sorted({p.source for p in rows})),
            excel_rows=len(rows),
            missing_code=True,
            missing_full_name=not is_full_name(name),
        )
        for (department, _), rows in without_code.items()
        for name in [_best_name(p.employee_name for p in rows)]
    )
    return sorted(
        found,
        key=lambda item: (
            not item.missing_code,
            item.departments,
            normalize_header(item.employee_name),
            item.employee_code or "",
        ),
    )


def group_by_department(
    employees: Iterable[EmployeeDay],
) -> dict[str, list[EmployeeDay]]:
    """Each department's people for a day; someone who moved appears under each department."""
    groups: defaultdict[str, list[EmployeeDay]] = defaultdict(list)
    for employee in employees:
        for department in sorted({line.department for line in employee.lines}):
            groups[department].append(employee)
    return dict(sorted(groups.items()))


@dataclass(frozen=True)
class NameVariant:
    """One of the full names written for a shared code, and where it was written."""

    name: str
    sources: tuple[str, ...]
    departments: tuple[str, ...]


def find_name_conflicts(
    outputs: Iterable[WorkerOutput],
) -> dict[str, list[NameVariant]]:
    """Codes attached to different full names, which would merge two people's output.

    Each name keeps the files it came from, so the operator can open them and compare.
    """
    names: defaultdict[str, dict[str, str]] = defaultdict(dict)
    sources: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    departments: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    for output in outputs:
        if not is_full_name(output.employee_name):
            continue
        key = (output.employee_code, normalize_header(output.employee_name))
        names[output.employee_code].setdefault(key[1], output.employee_name)
        sources[key].add(output.source)
        departments[key].add(output.department)
    return {
        code: sorted(
            (
                NameVariant(
                    name=name,
                    sources=tuple(sorted(sources[(code, normalized)])),
                    departments=tuple(sorted(departments[(code, normalized)])),
                )
                for normalized, name in variants.items()
            ),
            key=lambda variant: variant.name,
        )
        for code, variants in names.items()
        if len(variants) > 1
    }


def format_quantity(quantity: float, decimals: int = 2) -> str:
    """Vietnamese number style: 1.234,5."""
    whole, _, fraction = f"{quantity:,.{decimals}f}".partition(".")
    fraction = fraction.rstrip("0")
    whole = whole.replace(",", ".")
    return f"{whole},{fraction}" if fraction else whole


def _with_unit(quantity: float, unit: str | None) -> str:
    text = format_quantity(quantity)
    return f"{text} {unit}" if unit else text


def format_line(line: EmployeeLine) -> str:
    process = f" {line.process}" if line.process else ""
    return (
        f"{line.department}{process} ({line.role}): "
        f"{_with_unit(line.quantity, line.unit)}"
    )


def _shortened(text: str | None) -> str:
    if not text:
        return ""
    return f"{text[:MAX_ITEM_CHARS]}..." if len(text) > MAX_ITEM_CHARS else text


def share_calculation(detail: DetailLine) -> str:
    """The left side of the sum, so a person can redo it: SL tổng × hệ số."""
    overtime = detail.detail.overtime_quantity
    if overtime:
        regular = detail.base_quantity - overtime
        base = f"[{format_quantity(regular)} + {format_quantity(overtime)} (tăng ca)]"
    elif detail.detail.overtime:
        base = f"{format_quantity(detail.base_quantity)} (SL tổng, tăng ca)"
    else:
        base = f"{format_quantity(detail.base_quantity)} (SL tổng)"
    return f"{base} x {format_quantity(detail.ratio, RATIO_DECIMALS)} (hệ số)"


def _format_share(detail: DetailLine, markers: Markers) -> str:
    """Written as the sum it is: SL tổng × hệ số = SL thực nhận."""
    return (
        f"{share_calculation(detail)} = "
        f"{markers.result}{format_quantity(detail.quantity)} (SL thực nhận)"
    )


def _detail_parts(detail: DetailLine, markers: Markers) -> list[str]:
    """What the row is: shift, machine, item and step, laid out for the style."""
    shift = f"Ca {detail.detail.shift}" if detail.detail.shift else ""
    machine = f"máy {detail.detail.machine}" if detail.detail.machine else ""
    item = _shortened(detail.detail.item)
    step = _shortened(detail.detail.step)
    if not markers.item_on_own_line:
        head = ", ".join(part for part in (shift, machine, item) if part)
        return [part for part in (head, step) if part]
    head = " · ".join(
        f"{marker}{part[0].upper()}{part[1:]}"
        for marker, part in ((markers.shift, shift), (markers.machine, machine))
        if part
    )
    return [
        part
        for part in (
            head,
            f"{markers.item}{item}" if item else "",
            f"{markers.step}{step}" if step else "",
        )
        if part
    ]


def _format_detail(number: int, detail: DetailLine, markers: Markers) -> str:
    """A numbered block, so a person can point at the row they question."""
    described = _detail_parts(detail, markers)
    share = _format_share(detail, markers)
    lines = (
        [f"{number}) {described[0]}", *described[1:], share]
        if described
        else [f"{number}) {share}"]
    )
    return "\n".join([lines[0], *(DETAIL_INDENT + line for line in lines[1:])])


def department_title(line: EmployeeLine) -> str:
    process = f" {line.process}" if line.process else ""
    return f"{line.department}{process} – {line.role}"


def _department_header(line: EmployeeLine, markers: Markers) -> str:
    return (
        f"{markers.department}{department_title(line)}: "
        f"{_with_unit(line.quantity, line.unit)}"
    )


def day_totals(employee: EmployeeDay) -> str:
    """The day's total per unit, e.g. "100 cái · 8 kg"."""
    totals: dict[str | None, float] = {}
    for line in employee.lines:
        totals[line.unit] = totals.get(line.unit, 0.0) + line.quantity
    return " · ".join(_with_unit(q, unit) for unit, q in totals.items())


def _summary(employee: EmployeeDay, markers: Markers) -> str:
    """The day's total first, kept apart per unit because kg and pieces do not add up."""
    totals: dict[str | None, float] = {}
    overtime: dict[str | None, float] = {}
    flagged_only = False
    for line in employee.lines:
        totals[line.unit] = totals.get(line.unit, 0.0) + line.quantity
        for detail in line.details:
            overtime[line.unit] = overtime.get(line.unit, 0.0) + detail.overtime_share
            flagged_only |= detail.detail.overtime and not detail.overtime_share
    rows = [f"{markers.total}TỔNG: {day_totals(employee)}"]
    # A row marked overtime without its overtime output would make "giờ thường" wrong.
    if any(overtime.values()) and not flagged_only:
        several = len(totals) > 1
        rows.extend(
            f"{markers.split}Giờ thường {_with_unit(totals[unit] - ot, unit if several else None)}"
            f" · Tăng ca {_with_unit(ot, unit if several else None)}"
            for unit, ot in overtime.items()
            if ot
        )
    rows.append(SEPARATOR)
    return "\n".join(rows)


def _message_blocks(employee: EmployeeDay, markers: Markers) -> list[str]:
    """Headings travel with the row they head, so a part never ends on a bare heading."""
    blocks: list[str] = []
    number = 0
    for line in employee.lines:
        heading = _department_header(line, markers)
        for detail in line.details:
            number += 1
            text = _format_detail(number, detail, markers)
            blocks.append(f"{heading}\n{text}" if heading else text)
            heading = ""
        if heading:
            blocks.append(heading)
    summary = _summary(employee, markers)
    if blocks:
        blocks[0] = f"{summary}\n{blocks[0]}"
    else:
        blocks.append(summary)
    return blocks


def _fit_into_parts(blocks: Iterable[str], room: int) -> list[list[str]]:
    """Whole blocks only, so a row's quantity never lands apart from what it describes."""
    parts: list[list[str]] = [[]]
    used = 0
    for block in blocks:
        cost = message_length(block) + len(BLOCK_GAP)
        if parts[-1] and used + cost > room:
            parts.append([])
            used = 0
        parts[-1].append(block)
        used += cost
    return parts


def build_messages(
    employee: EmployeeDay, style: TextStyle = TextStyle.PLAIN
) -> list[str]:
    """The text to send, split between blocks when it would exceed what Zalo accepts."""
    markers = MARKERS[style]
    title = f"{markers.title}Sản lượng ngày {employee.day:%d/%m/%Y}"
    person = f"{markers.person}{employee.employee_name} – MS {employee.employee_code}"
    # Leaves room for the title, the person and a "(phần 10/10)" marker on every part.
    room = (
        MAX_MESSAGE_CHARS
        - message_length(title)
        - message_length(person)
        - len(" (phần 10/10)\n\n")
    )
    parts = _fit_into_parts(_message_blocks(employee, markers), room)
    return [
        "\n".join(
            [
                f"{title} (phần {number}/{len(parts)})" if len(parts) > 1 else title,
                person,
                BLOCK_GAP.join(part),
            ]
        )
        for number, part in enumerate(parts, start=1)
    ]
