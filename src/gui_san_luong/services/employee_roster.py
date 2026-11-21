"""Full names from a workbook's timesheet, for people the report rows name only by nickname.

Every report keeps a timesheet listing each person's nickname, code and full name. Output is
still credited by code; the timesheet only supplies the full name shown for that code.
"""

import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from gui_san_luong.services.excel_import import normalize_header, open_workbook
from gui_san_luong.services.worker_output import (
    WorkerOutput,
    is_full_name,
    normalize_code,
)

# Normalized sheet names in order of preference, as the user asked: "BANG CONG", then "BCC".
ROSTER_SHEETS = ("bang cong", "bcc")
CODE_HEADERS = frozenset({"ma so", "ma cn", "mnv"})
NAME_HEADERS = frozenset({"ho ten", "ho va ten"})


def _name_key(name: str) -> str:
    """Accents are kept: "Hoàng" and "Hoàn" are different names, not a spelling variant."""
    return " ".join(unicodedata.normalize("NFC", name).casefold().split())


@dataclass(frozen=True)
class Roster:
    sheet: str
    names: dict[str, str]
    # Codes written with different full names; none of them is used, rather than guessing.
    conflicting_codes: tuple[str, ...] = ()


def find_roster_sheet(sheet_names: Iterable[str]) -> str | None:
    by_name = {normalize_header(name): name for name in sheet_names}
    return next(
        (by_name[wanted] for wanted in ROSTER_SHEETS if wanted in by_name), None
    )


def _header_columns(row: Sequence[Any]) -> tuple[int, int] | None:
    """The code and full-name columns, when this row is a timesheet header."""
    headers = {
        normalize_header(value): column
        for column, value in enumerate(row)
        if isinstance(value, str)
    }
    code = next((headers[h] for h in CODE_HEADERS if h in headers), None)
    name = next((headers[h] for h in NAME_HEADERS if h in headers), None)
    return None if code is None or name is None else (code, name)


def parse_roster(sheet: str, rows: Iterable[Sequence[Any]]) -> Roster:
    """Read code and full name below each header; a sheet may stack several tables."""
    found: defaultdict[str, dict[str, str]] = defaultdict(dict)
    columns: tuple[int, int] | None = None
    for row in rows:
        if header := _header_columns(row):
            columns = header
            continue
        if columns is None:
            continue
        code_column, name_column = columns
        if max(columns) >= len(row):
            continue
        code = normalize_code(row[code_column])
        name = row[name_column]
        if not code or not isinstance(name, str) or not is_full_name(name):
            continue
        name = " ".join(name.split())
        found[code].setdefault(_name_key(name), name)
    names = {code: next(iter(v.values())) for code, v in found.items() if len(v) == 1}
    conflicts = tuple(sorted(code for code, v in found.items() if len(v) > 1))
    return Roster(sheet, names, conflicts)


def read_roster(data: bytes) -> Roster | None:
    """The workbook's timesheet names, or None when it has no timesheet sheet."""
    with open_workbook(data, data_only=True) as workbook:
        sheet = find_roster_sheet(workbook.sheetnames)
        if sheet is None:
            return None
        return parse_roster(sheet, workbook[sheet].iter_rows(values_only=True))


def _roster_name(code: str, source: str, rosters: Mapping[str, Roster]) -> str | None:
    own = rosters.get(source)
    if own and code in own.names:
        return own.names[code]
    # Another file's timesheet is used only when every one that lists the code agrees.
    others = {
        _name_key(name): name
        for other_source, roster in rosters.items()
        if other_source != source and (name := roster.names.get(code))
    }
    return next(iter(others.values())) if len(others) == 1 else None


def fill_full_names(
    outputs: Iterable[WorkerOutput], rosters: Mapping[str, Roster]
) -> list[WorkerOutput]:
    """Replace nicknames with the timesheet's full name for the same code.

    A full name already written in the report is kept, so a timesheet that disagrees with it
    shows up as a name conflict instead of silently winning.
    """
    filled = []
    for output in outputs:
        name = (
            None
            if is_full_name(output.employee_name)
            else _roster_name(output.employee_code, output.source, rosters)
        )
        filled.append(replace(output, employee_name=name) if name else output)
    return filled
