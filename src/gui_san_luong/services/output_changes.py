"""Notice whose output changed when a report is edited in Excel and uploaded again.

An operator who corrects a wrong figure must send that person a new message, and can easily forget
whose figures they touched. The last version of each report is kept, so a new version is compared
line by line with it, and every difference stays listed until the operator marks it handled.
"""

import hashlib
import json
import logging
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path, PurePath

from gui_san_luong.db.connection import open_database
from gui_san_luong.db.output_changes import (
    NewChange,
    SnapshotRecord,
    fetch_open_changes,
    fetch_snapshot,
    mark_handled,
    save_version,
)
from gui_san_luong.services.worker_output import WorkerOutput

STORAGE_ERROR = "Không đọc hoặc ghi được lịch sử thay đổi sản lượng. Hãy thử lại."
logger = logging.getLogger(__name__)
# Quantities are shares of a row's output, so the same figure may differ in the last digits.
QUANTITY_TOLERANCE = 1e-6
QUANTITY_DECIMALS = 6
# Downloading the same report again names it "file (1).xlsx" or "file - Copy.xlsx".
COPY_SUFFIX = re.compile(r"\s*(?:\(\d+\)|-\s*copy|-\s*bản sao)$")
LINE_SEPARATOR = " · "


class ChangeStorageError(Exception):
    """A user-facing reason why the change history could not be read or saved."""


class VersionOutcome(StrEnum):
    FIRST_SEEN = "first_seen"
    UNCHANGED = "unchanged"
    CHANGED = "changed"


@dataclass(frozen=True)
class OutputEntry:
    """An employee's output on one Excel row, under the line a message shows it on.

    Several rows can share a line (two runs on the same machine and shift), but the operator
    edits rows, so each row is compared and listed on its own.
    """

    day: date
    employee_code: str
    employee_name: str
    line: str
    unit: str | None
    quantity: float
    # None in versions saved before rows were kept: the quantity is then the line's total.
    excel_row: int | None = None

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.day.isoformat(), self.employee_code, self.line, self.unit or "")


@dataclass(frozen=True)
class TrackedFile:
    source_key: str
    file_name: str
    file_digest: str
    outputs: Sequence[WorkerOutput]


@dataclass(frozen=True)
class RecordedVersion:
    outcome: VersionOutcome
    changes: int = 0


@dataclass(frozen=True)
class OutputChange:
    id: int
    detected_at: datetime
    file_name: str
    day: date
    employee_code: str
    employee_name: str
    line: str
    unit: str | None
    old_quantity: float | None
    new_quantity: float | None
    excel_row: int | None = None

    @property
    def difference(self) -> float:
        return (self.new_quantity or 0.0) - (self.old_quantity or 0.0)


def source_key(template_key: str, file_name: str) -> str:
    """The same report under a new download name is still the same report."""
    stem = unicodedata.normalize("NFC", PurePath(file_name).stem).casefold().strip()
    return f"{template_key}:{COPY_SUFFIX.sub('', stem)}"


def _line_label(output: WorkerOutput) -> str:
    detail = output.detail
    process = f" {output.process}" if output.process else ""
    parts = [
        f"{output.department}{process} ({output.role})",
        detail.item or "",
        f"máy {detail.machine}" if detail.machine else "",
        f"ca {detail.shift}" if detail.shift else "",
        detail.step or "",
        "tăng ca" if detail.overtime else "",
    ]
    return LINE_SEPARATOR.join(part for part in parts if part)


def output_entries(outputs: Iterable[WorkerOutput]) -> list[OutputEntry]:
    quantities: defaultdict[
        tuple[date, str, str, str | None, int | None], list[float]
    ] = defaultdict(list)
    names: defaultdict[str, set[str]] = defaultdict(set)
    for output in outputs:
        key = (
            output.day,
            output.employee_code,
            _line_label(output),
            output.unit,
            output.excel_row,
        )
        quantities[key].append(output.quantity)
        names[output.employee_code].add(output.employee_name)
    return [
        OutputEntry(
            day,
            code,
            max(names[code], key=lambda name: (len(name), name)),
            line,
            unit,
            math.fsum(values),
            row,
        )
        for (day, code, line, unit, row), values in sorted(
            quantities.items(),
            key=lambda item: (*map(str, item[0][:4]), item[0][4] or 0),
        )
    ]


def entries_digest(entries: Iterable[OutputEntry]) -> str:
    """Names are left out: a full name found later is not a change in anyone's output."""
    canonical = sorted(
        (*entry.identity, round(entry.quantity, QUANTITY_DECIMALS)) for entry in entries
    )
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def _encode(entries: Iterable[OutputEntry]) -> str:
    return json.dumps(
        [
            [
                entry.day.isoformat(),
                entry.employee_code,
                entry.employee_name,
                entry.line,
                entry.unit,
                entry.quantity,
                entry.excel_row,
            ]
            for entry in entries
        ],
        ensure_ascii=False,
    )


def _decode(text: str) -> list[OutputEntry]:
    entries = []
    for day, code, name, line, unit, quantity, *row in json.loads(text):
        entries.append(
            OutputEntry(
                date.fromisoformat(day),
                code,
                name,
                line,
                unit,
                float(quantity),
                row[0] if row else None,
            )
        )
    return entries


def _same(old: float, new: float) -> bool:
    return math.isclose(old, new, abs_tol=QUANTITY_TOLERANCE)


RowPair = tuple[OutputEntry | None, OutputEntry | None]


def _pair_rows(was: list[OutputEntry], now: list[OutputEntry]) -> list[RowPair]:
    """Pair the rows of one line between versions, so an edited row is compared with itself.

    Row numbers shift when rows are inserted or deleted above, but rows keep their order, so
    rows are paired in order. When rows were added or removed, rows whose figure is unchanged
    are matched first, so only the added, removed or edited rows are reported.
    """
    if len(was) == len(now):
        return list(zip(was, now, strict=True))
    unmatched = list(now)
    pairs: list[RowPair] = []
    left: list[OutputEntry] = []
    for old in was:
        match = next(
            (new for new in unmatched if _same(old.quantity, new.quantity)), None
        )
        if match is None:
            left.append(old)
        else:
            unmatched.remove(match)
            pairs.append((old, match))
    for index in range(max(len(left), len(unmatched))):
        pairs.append(
            (
                left[index] if index < len(left) else None,
                unmatched[index] if index < len(unmatched) else None,
            )
        )
    return pairs


def _row_order(pair: RowPair) -> int:
    entry = pair[1] or pair[0]
    assert entry is not None
    return entry.excel_row or 0


def _by_line(
    entries: Iterable[OutputEntry],
) -> dict[tuple[str, str, str, str], list[OutputEntry]]:
    lines: defaultdict[tuple[str, str, str, str], list[OutputEntry]] = defaultdict(list)
    for entry in entries:
        lines[entry.identity].append(entry)
    for rows in lines.values():
        rows.sort(key=lambda entry: entry.excel_row or 0)
    return lines


def _line_total(rows: list[OutputEntry]) -> list[OutputEntry]:
    if not rows:
        return []
    total = math.fsum(row.quantity for row in rows)
    return [replace(rows[0], quantity=total, excel_row=None)]


def compare_entries(
    old: Iterable[OutputEntry], new: Iterable[OutputEntry]
) -> list[NewChange]:
    before = _by_line(old)
    after = _by_line(new)
    changes = []
    for identity in sorted(before.keys() | after.keys()):
        was, now = before.get(identity, []), after.get(identity, [])
        if any(row.excel_row is None for row in (*was, *now)):
            # A version saved before rows were kept only knows the line's total.
            was, now = _line_total(was), _line_total(now)
        for old_row, new_row in sorted(_pair_rows(was, now), key=_row_order):
            if (
                old_row is not None
                and new_row is not None
                and _same(old_row.quantity, new_row.quantity)
            ):
                continue
            entry = new_row or old_row
            assert entry is not None
            changes.append(
                NewChange(
                    entry.day.isoformat(),
                    entry.employee_code,
                    entry.employee_name,
                    entry.line,
                    entry.unit,
                    old_row.quantity if old_row else None,
                    new_row.quantity if new_row else None,
                    entry.excel_row,
                )
            )
    return changes


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_version(path: Path, tracked: TrackedFile) -> RecordedVersion:
    """Compare an uploaded report with its last version and log what changed.

    Only a different file counts: the same file read with other columns or another start row
    moves the baseline quietly, since that is a correction of the reading, not of the figures.
    """
    entries = output_entries(tracked.outputs)
    if not entries:
        # Nothing could be credited, most likely a reading problem: keep the last good version.
        return RecordedVersion(VersionOutcome.UNCHANGED)
    digest = entries_digest(entries)
    snapshot = SnapshotRecord(
        tracked.source_key,
        tracked.file_name,
        tracked.file_digest,
        digest,
        _encode(entries),
    )
    try:
        with open_database(path) as connection:
            previous = fetch_snapshot(connection, tracked.source_key)
            if previous is None:
                save_version(connection, snapshot, [], _now())
                return RecordedVersion(VersionOutcome.FIRST_SEEN)
            if previous.outputs_digest == digest:
                if previous.file_digest != tracked.file_digest:
                    save_version(connection, snapshot, [], _now())
                return RecordedVersion(VersionOutcome.UNCHANGED)
            if previous.file_digest == tracked.file_digest:
                save_version(connection, snapshot, [], _now())
                return RecordedVersion(VersionOutcome.UNCHANGED)
            changes = compare_entries(_decode(previous.outputs_json), entries)
            save_version(connection, snapshot, changes, _now())
    except sqlite3.Error as error:
        raise _storage_error(error) from error
    return RecordedVersion(VersionOutcome.CHANGED, len(changes))


def _storage_error(error: sqlite3.Error) -> ChangeStorageError:
    """The page shows only a short message, so the SQLite cause (e.g. no write permission
    on the data folder) goes to the server log."""
    logger.warning("Output change history could not be read or written", exc_info=error)
    return ChangeStorageError(STORAGE_ERROR)


def load_open_changes(path: Path) -> list[OutputChange]:
    try:
        with open_database(path) as connection:
            records = fetch_open_changes(connection)
    except sqlite3.Error as error:
        raise _storage_error(error) from error
    return [
        OutputChange(
            record.id,
            datetime.fromisoformat(record.detected_at),
            record.file_name,
            date.fromisoformat(record.day),
            record.employee_code,
            record.employee_name,
            record.line,
            record.unit,
            record.old_quantity,
            record.new_quantity,
            record.excel_row,
        )
        for record in records
    ]


def mark_changes_handled(path: Path, change_ids: Sequence[int]) -> None:
    try:
        with open_database(path) as connection:
            mark_handled(connection, change_ids, _now())
    except sqlite3.Error as error:
        raise _storage_error(error) from error
