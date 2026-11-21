"""Spotting whose output changed between two versions of a report, using a throwaway database."""

from datetime import date
from pathlib import Path

import pytest

from gui_san_luong.services.output_changes import (
    TrackedFile,
    VersionOutcome,
    load_open_changes,
    mark_changes_handled,
    record_version,
    source_key,
)
from gui_san_luong.services.worker_output import OutputDetail, WorkerOutput

DAY = date(2030, 1, 1)


def _output(
    code: str,
    quantity: float,
    name: str = "Nguyễn Văn A",
    item: str = "Hàng X",
    row: int | None = None,
) -> WorkerOutput:
    return WorkerOutput(
        employee_code=code,
        employee_name=name,
        source="cat.xlsx",
        department="Cắt",
        role="NV01",
        process=None,
        day=DAY,
        quantity=quantity,
        unit="cái",
        base_quantity=quantity,
        detail=OutputDetail(item=item),
        excel_row=row,
    )


def _record(path: Path, digest: str, *outputs: WorkerOutput) -> VersionOutcome:
    return record_version(
        path, TrackedFile("cat:cat", "cat.xlsx", digest, list(outputs))
    ).outcome


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def test_first_version_is_only_a_baseline(db: Path) -> None:
    assert _record(db, "v1", _output("1", 10)) is VersionOutcome.FIRST_SEEN
    assert load_open_changes(db) == []


def test_edited_figures_are_listed_with_old_and_new_values(db: Path) -> None:
    _record(db, "v1", _output("1", 10), _output("2", 5), _output("3", 7))
    outcome = _record(db, "v2", _output("1", 12), _output("3", 7), _output("4", 3))

    assert outcome is VersionOutcome.CHANGED
    changes = {
        change.employee_code: (change.old_quantity, change.new_quantity)
        for change in load_open_changes(db)
    }
    assert changes == {"1": (10, 12), "2": (5, None), "4": (None, 3)}


def test_output_moved_between_items_is_a_change_even_with_the_same_total(
    db: Path,
) -> None:
    _record(db, "v1", _output("1", 10, item="Hàng X"))
    _record(db, "v2", _output("1", 10, item="Hàng Y"))

    assert {change.line for change in load_open_changes(db)} == {
        "Cắt (NV01) · Hàng X",
        "Cắt (NV01) · Hàng Y",
    }


def test_same_file_read_with_other_settings_moves_the_baseline_quietly(
    db: Path,
) -> None:
    _record(db, "v1", _output("1", 10))
    assert _record(db, "v1", _output("1", 99)) is VersionOutcome.UNCHANGED
    assert load_open_changes(db) == []

    # The next edit compares with what the corrected reading showed.
    _record(db, "v2", _output("1", 100))
    assert [(c.old_quantity, c.new_quantity) for c in load_open_changes(db)] == [
        (99, 100)
    ]


def test_a_saved_file_with_the_same_figures_or_a_new_full_name_is_no_change(
    db: Path,
) -> None:
    _record(db, "v1", _output("1", 10, name="TRUYEN"))
    assert (
        _record(db, "v2", _output("1", 10.0000001, name="Trần Thị Truyện"))
        is VersionOutcome.UNCHANGED
    )
    assert load_open_changes(db) == []


def test_a_version_with_nothing_credited_keeps_the_last_good_one(db: Path) -> None:
    _record(db, "v1", _output("1", 10))
    assert _record(db, "v2") is VersionOutcome.UNCHANGED
    _record(db, "v3", _output("1", 10))
    assert load_open_changes(db) == []


def test_handled_changes_are_no_longer_listed(db: Path) -> None:
    _record(db, "v1", _output("1", 10), _output("2", 5))
    _record(db, "v2", _output("1", 11), _output("2", 6))
    first, second = load_open_changes(db)

    mark_changes_handled(db, [first.id])

    assert [change.id for change in load_open_changes(db)] == [second.id]


def test_a_new_download_of_the_same_report_keeps_its_history() -> None:
    assert source_key("cat", "Cắt T1.xlsx") == source_key("cat", "cắt t1 (1).xlsx")
    assert source_key("cat", "Cắt T1.xlsx") == source_key("cat", "Cắt T1 - Copy.xlsx")
    assert source_key("cat", "Cắt T1.xlsx") != source_key("in", "Cắt T1.xlsx")
    assert source_key("cat", "Cắt T1.xlsx") != source_key("cat", "Cắt T2.xlsx")


def _row_changes(db: Path) -> list[tuple[int | None, float | None, float | None]]:
    return [
        (c.excel_row, c.old_quantity, c.new_quantity) for c in load_open_changes(db)
    ]


def test_each_edited_row_is_listed_even_when_rows_share_a_message_line(
    db: Path,
) -> None:
    _record(db, "v1", _output("1", 1070, row=10), _output("1", 175, row=11))
    _record(db, "v2", _output("1", 123, row=10), _output("1", 123, row=11))

    assert _row_changes(db) == [(10, 1070, 123), (11, 175, 123)]


def test_rows_shifted_by_an_inserted_row_are_not_reported(db: Path) -> None:
    _record(db, "v1", _output("1", 50, row=10), _output("1", 70, row=11))
    _record(
        db,
        "v2",
        _output("1", 50, row=11),
        _output("1", 30, row=12),
        _output("1", 70, row=13),
    )

    assert _row_changes(db) == [(12, None, 30)]


def test_a_version_saved_without_rows_is_compared_by_line_total(db: Path) -> None:
    _record(db, "v1", _output("1", 1245))
    _record(db, "v2", _output("1", 1070, row=10), _output("1", 175, row=11))
    assert load_open_changes(db) == []

    _record(db, "v3", _output("1", 123, row=10), _output("1", 175, row=11))
    assert _row_changes(db) == [(10, 1070, 123)]
