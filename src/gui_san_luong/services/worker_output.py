"""Credit each employee with their share of output, across every imported report.

People move between departments within a month, so one employee may appear in several files.
The employee code links them: nicknames repeat across departments and cannot identify anyone.
"""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
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


@dataclass(frozen=True)
class OutputDetail:
    item: str | None = None
    machine: str | None = None
    shift: str | None = None
    overtime: bool = False
    step: str | None = None


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


@dataclass(frozen=True)
class CreditProblem:
    source: str
    excel_row: int
    day: date
    reason: str


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
    )


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


def find_name_conflicts(outputs: Iterable[WorkerOutput]) -> dict[str, list[str]]:
    """Codes attached to different full names, which would merge two people's output."""
    full_names: defaultdict[str, dict[str, str]] = defaultdict(dict)
    for output in outputs:
        normalized = normalize_header(output.employee_name)
        if len(normalized.split()) >= MIN_FULL_NAME_WORDS:
            full_names[output.employee_code][normalized] = output.employee_name
    return {
        code: sorted(names.values())
        for code, names in full_names.items()
        if len(names) > 1
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


def _format_detail(detail: DetailLine, unit: str | None) -> str:
    parts = [
        f"Ca {detail.detail.shift}" if detail.detail.shift else "",
        f"máy {detail.detail.machine}" if detail.detail.machine else "",
        _shortened(detail.detail.item),
        _shortened(detail.detail.step),
    ]
    described = ", ".join(part for part in parts if part)
    amounts = (
        f"{_with_unit(detail.quantity, unit)} "
        f"(SL chính {format_quantity(detail.base_quantity)}, "
        f"hệ số {format_quantity(detail.ratio, RATIO_DECIMALS)})"
    )
    overtime = ", tăng ca" if detail.detail.overtime else ""
    return (
        f"- {described}: {amounts}{overtime}" if described else f"- {amounts}{overtime}"
    )


def _message_lines(employee: EmployeeDay) -> list[str]:
    lines: list[str] = []
    for line in employee.lines:
        lines.append(format_line(line))
        lines.extend(_format_detail(detail, line.unit) for detail in line.details)
    return lines


def _fit_into_parts(lines: Iterable[str], room: int) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    used = 0
    for line in lines:
        cost = len(line) + 1
        if parts[-1] and used + cost > room:
            parts.append([])
            used = 0
        parts[-1].append(line)
        used += cost
    return parts


def build_messages(employee: EmployeeDay) -> list[str]:
    """The text to send, split between lines when it would exceed what Zalo accepts."""
    title = f"Sản lượng ngày {employee.day:%d/%m/%Y}"
    person = f"{employee.employee_name} (MS {employee.employee_code})"
    # Leaves room for the title, the person and a "(phần 10/10)" marker on every part.
    room = MAX_MESSAGE_CHARS - len(title) - len(person) - len(" (phần 10/10)\n\n\n")
    parts = _fit_into_parts(_message_lines(employee), room)
    return [
        "\n".join(
            [
                f"{title} (phần {number}/{len(parts)})" if len(parts) > 1 else title,
                person,
                "",
                *part,
            ]
        )
        for number, part in enumerate(parts, start=1)
    ]
