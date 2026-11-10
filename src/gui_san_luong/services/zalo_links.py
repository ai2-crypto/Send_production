"""Decide and save which Zalo account receives each employee's output.

A wrong link sends one person's figures to someone else and a sent message cannot be recalled,
so a table with any problem is refused whole. A link alone does not allow sending: permission
to receive figures remains a separate approval.
"""

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gui_san_luong.db.connection import open_database
from gui_san_luong.db.zalo_links import LinkRecord, fetch_links, replace_links
from gui_san_luong.services.worker_output import EmployeeIdentity, normalize_code

ZALO_USER_ID_PATTERN = re.compile(r"[0-9]{1,32}")
STORAGE_ERROR = "Không đọc hoặc ghi được dữ liệu nối Zalo. Hãy thử lại."


class ZaloLinkStorageError(Exception):
    """A user-facing reason why links could not be read or saved."""


class InvalidLinksError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class LinkRow:
    """One row as typed by the user, not yet checked."""

    employee_code: object
    employee_name: object
    zalo_user_id: object


@dataclass(frozen=True)
class EditableRow:
    employee_code: str
    employee_name: str
    zalo_user_id: str
    departments: str


@dataclass(frozen=True)
class LinkChanges:
    upserts: list[LinkRecord]
    removals: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.upserts and not self.removals


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def plan_changes(stored: Iterable[LinkRecord], rows: Iterable[LinkRow]) -> LinkChanges:
    """Compare the edited table with what is stored. Raises when any row is unsafe to save."""
    problems: list[str] = []
    wanted: dict[str, LinkRecord] = {}
    seen_codes: set[str] = set()
    owners: dict[str, str] = {}

    for row in rows:
        code = normalize_code(row.employee_code)
        zalo_user_id = _clean(row.zalo_user_id)
        if code is None:
            if zalo_user_id:
                problems.append(f"Zalo user id {zalo_user_id} chưa có mã nhân viên")
            continue
        if code in seen_codes:
            problems.append(f"Mã {code} xuất hiện nhiều dòng")
            continue
        seen_codes.add(code)
        if not zalo_user_id:
            continue
        if not ZALO_USER_ID_PATTERN.fullmatch(zalo_user_id):
            problems.append(f"Mã {code}: Zalo user id chỉ được gồm chữ số")
            continue
        if zalo_user_id in owners:
            problems.append(
                f"Mã {owners[zalo_user_id]} và {code} dùng chung một Zalo user id"
            )
            continue
        owners[zalo_user_id] = code
        wanted[code] = LinkRecord(code, _clean(row.employee_name) or code, zalo_user_id)

    if problems:
        raise InvalidLinksError(problems)

    current = {link.employee_code: link for link in stored}
    return LinkChanges(
        upserts=[link for code, link in wanted.items() if current.get(code) != link],
        removals=[code for code in current if code not in wanted],
    )


def build_editable_rows(
    stored: Iterable[LinkRecord], roster: Iterable[EmployeeIdentity]
) -> list[EditableRow]:
    """Everyone imported or already linked; a saved link outlives the file that showed the person."""
    links = {link.employee_code: link for link in stored}
    known = {employee.employee_code: employee for employee in roster}
    rows = []
    for code in sorted(links.keys() | known.keys()):
        link = links.get(code)
        employee = known.get(code)
        name = (
            employee.employee_name if employee else link.employee_name if link else ""
        )
        rows.append(
            EditableRow(
                employee_code=code,
                employee_name=name,
                zalo_user_id=link.zalo_user_id if link else "",
                departments=", ".join(employee.departments) if employee else "",
            )
        )
    return rows


def load_links(path: Path) -> list[LinkRecord]:
    try:
        with open_database(path) as connection:
            return fetch_links(connection)
    except sqlite3.Error as error:
        raise ZaloLinkStorageError(STORAGE_ERROR) from error


def save_links(path: Path, rows: Sequence[LinkRow], actor: str) -> LinkChanges:
    try:
        with open_database(path) as connection:
            changes = plan_changes(fetch_links(connection), rows)
            if not changes.is_empty:
                replace_links(
                    connection,
                    changes.upserts,
                    changes.removals,
                    actor,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                )
    except sqlite3.Error as error:
        raise ZaloLinkStorageError(STORAGE_ERROR) from error
    return changes
