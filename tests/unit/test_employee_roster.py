"""Timesheet name lookup on synthetic workbooks; no real employee names belong here."""

from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import Workbook

from gui_san_luong.services.employee_roster import (
    Roster,
    fill_full_names,
    find_roster_sheet,
    parse_roster,
    read_roster,
)
from gui_san_luong.services.worker_output import WorkerOutput

HEADER = ["STT", "Tên", "Mã\nsố", "HỌ & TÊN", datetime(2030, 1, 1)]


def _workbook(sheets: dict[str, list[list[Any]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)  # type: ignore[arg-type]
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _output(code: str, name: str, source: str = "a.xlsx") -> WorkerOutput:
    return WorkerOutput(
        employee_code=code,
        employee_name=name,
        source=source,
        department="In",
        role="Đứng máy",
        process=None,
        day=date(2030, 1, 1),
        quantity=1.0,
        unit=None,
        base_quantity=1.0,
    )


def test_bang_cong_is_preferred_over_bcc() -> None:
    assert find_roster_sheet(["CHI TIET", "BCC", "BANG CONG"]) == "BANG CONG"
    assert find_roster_sheet(["CHI TIET", "BCC"]) == "BCC"
    assert find_roster_sheet(["CHI TIET", "BCC tang ca"]) is None


def test_roster_reads_every_stacked_table_below_its_header() -> None:
    rows = [
        ["BẢNG CHẤM CÔNG"],
        HEADER,
        ["1", "an", 10, "Lê Văn An", "x"],
        ["CỘNG", None, None, None],
        ["STT", "Tên", "MNV", "HỌ & TÊN"],
        ["1", "bo", 11.0, "  Trần   Thị Bơ ", "x"],
        ["2", "cu", 12, "cu"],
    ]
    roster = parse_roster("BCC", rows)
    assert roster.names == {"10": "Lê Văn An", "11": "Trần Thị Bơ"}


def test_code_with_two_different_full_names_is_not_used() -> None:
    rows = [
        HEADER,
        ["1", "an", 10, "Lê Văn An"],
        ["1", "an", 10, "Lê Văn Ân"],
        ["2", "bo", 11, "Trần Thị Bơ"],
        ["2", "bo", 11, "Trần Thị Bơ"],
    ]
    roster = parse_roster("BANG CONG", rows)
    assert roster.names == {"11": "Trần Thị Bơ"}
    assert roster.conflicting_codes == ("10",)


def test_read_roster_reads_past_the_preview_rows() -> None:
    people = [[str(i), "x", i, f"Người Số {i}"] for i in range(1, 120)]
    data = _workbook({"CHI TIET": [["NGÀY"]], "BANG CONG": [HEADER, *people]})
    roster = read_roster(data)
    assert roster is not None
    assert roster.sheet == "BANG CONG"
    assert roster.names["119"] == "Người Số 119"


def test_workbook_without_timesheet_has_no_roster() -> None:
    assert read_roster(_workbook({"CHI TIET": [["NGÀY"]]})) is None


def test_nickname_is_replaced_but_a_written_full_name_is_kept() -> None:
    rosters = {"a.xlsx": Roster("BANG CONG", {"10": "Lê Văn An", "11": "Trần Thị Bơ"})}
    filled = fill_full_names(
        [_output("10", "an"), _output("11", "Trần Thị Bờ"), _output("12", "cu")],
        rosters,
    )
    assert [o.employee_name for o in filled] == ["Lê Văn An", "Trần Thị Bờ", "cu"]


def test_underscored_nickname_is_replaced_whatever_its_case() -> None:
    rosters = {
        "a.xlsx": Roster("BANG CONG", {"64": "Lê Tuấn Anh", "65": "Phạm Văn Ba"})
    }
    filled = fill_full_names(
        [_output("64", "TUAN_ANH"), _output("65", "van_ba")], rosters
    )
    assert [o.employee_name for o in filled] == ["Lê Tuấn Anh", "Phạm Văn Ba"]


def test_own_files_timesheet_wins_and_others_are_used_only_when_they_agree() -> None:
    rosters = {
        "a.xlsx": Roster("BANG CONG", {"10": "Lê Văn An"}),
        "b.xlsx": Roster("BCC", {"10": "Lê Văn Ân", "11": "Trần Thị Bơ"}),
        "c.xlsx": Roster("BCC", {"11": "Trần Thị Bơ", "12": "Phạm Cù"}),
        "d.xlsx": Roster("BCC", {"12": "Phạm Củ"}),
    }
    filled = fill_full_names(
        [
            _output("10", "an", source="a.xlsx"),
            _output("11", "bo", source="a.xlsx"),
            _output("12", "cu", source="a.xlsx"),
        ],
        rosters,
    )
    assert [o.employee_name for o in filled] == ["Lê Văn An", "Trần Thị Bơ", "cu"]
