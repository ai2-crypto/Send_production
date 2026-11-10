"""Parser behaviour on synthetic workbooks; no real production data belongs here."""

from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from typing import Any

import pytest
from openpyxl import Workbook

from gui_san_luong.domain.production_templates import (
    CUTTING,
    PRINTING,
    SPINNING,
    WEAVING,
)
from gui_san_luong.services.excel_import import (
    ExcelImportError,
    MatchStatus,
    SheetPreview,
    analyze_sheet,
    column_letter,
    normalize_header,
    read_previews,
    read_rows,
    suggest_sheet,
    suggest_template,
)

DAY_1 = datetime(2030, 1, 1)
DAY_2 = datetime(2030, 1, 2)


def _workbook(
    rows: list[list[Any]],
    *,
    title: str = "NHAP LIEU",
    prepare: Callable[[Workbook], None] | None = None,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = title
    for row in rows:
        sheet.append(row)
    if prepare:
        prepare(workbook)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _cutting_rows(shift_by: int = 0) -> list[list[Any]]:
    """A cutting report: total above a two-row header, a helper index row, then data."""
    pad: list[Any] = [None] * shift_by
    return [
        pad + [None, None, None, None, None, None, 300],
        pad + [None] * 8,
        pad
        + [
            "Ngày\n SX",
            "CÔNG \nĐOẠN SX",
            "MÃ CÔNG \nĐOẠN",
            "SP - Tính lương",
            None,
            None,
            None,
            None,
            "S.LƯỢNG",
        ],
        pad
        + [
            None,
            None,
            None,
            "Tên CN \nNV01",
            "Tên CN \nNV02",
            "ĐƠN\n VỊ",
            "S.LƯỢNG",
            None,
            "ĐƠN\n VỊ (cái)",
        ],
        pad + [8, 10, 11, 12, None, 14, None, 15],
        pad + [DAY_1, "CAT_AUTO", "M1", "an", None, "PCS", 100, None, 100],
        pad + [DAY_1, "CAT_MANH", "M2", "binh", "chi", "PCS", 200, None, 200],
    ]


def _analyze(data: bytes, template: Any) -> Any:
    previews = read_previews(data)
    return previews, analyze_sheet(previews[0], template)


def _mapping(layout: Any) -> dict[str, int]:
    return {
        key: match.column
        for key, match in layout.matches.items()
        if match.status is MatchStatus.AUTO
    }


def _read(data: bytes, template: Any, sheet: str = "NHAP LIEU") -> Any:
    _, layout = _analyze(data, template)
    return read_rows(data, sheet, template, _mapping(layout), layout.data_start_row)


def test_normalize_header_handles_accents_newlines_and_punctuation() -> None:
    assert normalize_header("Ngày\n SX") == "ngay sx"
    assert normalize_header("S.LƯỢNG") == "s luong"
    assert normalize_header("N/Đ") == "n d"


def test_multi_row_header_and_index_row_are_recognized() -> None:
    _, layout = _analyze(_workbook(_cutting_rows()), CUTTING)
    assert layout.data_start_row == 6
    assert layout.matches["date"].column == 0
    assert layout.matches["worker_1"].column == 3


def test_exact_header_beats_longer_header_containing_the_same_words() -> None:
    _, layout = _analyze(_workbook(_cutting_rows()), CUTTING)
    assert column_letter(layout.matches["quantity"].column) == "G"
    assert column_letter(layout.matches["unit"].column) == "F"


@pytest.mark.parametrize("shift_by", [1, 3])
def test_inserted_columns_do_not_change_what_is_read(shift_by: int) -> None:
    baseline = _read(_workbook(_cutting_rows()), CUTTING)
    shifted = _read(_workbook(_cutting_rows(shift_by)), CUTTING)
    assert shifted.totals[0].counted == baseline.totals[0].counted == 300
    assert shifted.totals[0].matches_stated is True


def test_reordered_columns_are_still_found() -> None:
    rows = [
        ["S.LƯỢNG", "CÔNG ĐOẠN SX", "Ngày SX", "MÃ CÔNG ĐOẠN"],
        [5, "CAT_AUTO", DAY_1, "M1"],
    ]
    result = _read(_workbook(rows), CUTTING)
    assert result.rows[0]["quantity"] == 5


def test_same_header_is_separated_by_column_content() -> None:
    rows = [
        ["CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "NGÀY SX", "S.LƯỢNG", "ĐỨNG MÁY", "ĐỨNG MÁY"],
        ["IN_CAI", "M1", DAY_1, 7, "an", 0.25],
    ]
    _, layout = _analyze(_workbook(rows), PRINTING)
    assert layout.matches["operator"].column == 4


def test_two_equally_good_columns_are_left_for_the_user() -> None:
    rows = [
        ["CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "NGÀY SX", "S.LƯỢNG", "S.LƯỢNG"],
        ["IN_CAI", "M1", DAY_1, 7, 8],
    ]
    _, layout = _analyze(_workbook(rows), PRINTING)
    match = layout.matches["quantity"]
    assert match.status is MatchStatus.AMBIGUOUS
    assert match.column is None
    assert match.candidates == (3, 4)


def test_manual_mapping_overrides_detection() -> None:
    rows = [
        ["CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "NGÀY SX", "S.LƯỢNG", "S.LƯỢNG"],
        ["IN_CAI", "M1", DAY_1, 7, 8],
    ]
    mapping = {"process": 0, "process_code": 1, "date": 2, "quantity": 4}
    result = read_rows(_workbook(rows), "NHAP LIEU", PRINTING, mapping, 2)
    assert result.rows[0]["quantity"] == 8


def test_missing_required_column_is_an_error() -> None:
    rows = [["NGÀY SX"], [DAY_1]]
    with pytest.raises(ExcelImportError, match="Số lượng"):
        read_rows(_workbook(rows), "NHAP LIEU", CUTTING, {"date": 0}, 2)


def _reasons_for(cell: Any) -> list[str]:
    rows = [
        ["Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG"],
        [DAY_1, "A", "M1", 10],
        [DAY_1, "A", "M1", cell],
    ]
    result = _read(_workbook(rows), CUTTING)
    assert result.totals[0].counted == 10
    return [item.reason for item in result.rejected]


@pytest.mark.parametrize("cell", ["1.234", "1,234", "#REF!", "#N/A", -5])
def test_unusable_quantities_are_rejected_not_counted(cell: Any) -> None:
    assert len(_reasons_for(cell)) == 1


def test_empty_quantity_with_a_date_is_rejected_not_zero() -> None:
    assert "Thiếu Số lượng" in _reasons_for(None)


def test_zero_is_valid_and_blank_template_rows_are_skipped() -> None:
    rows = [
        ["Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG", "ĐƠN VỊ"],
        [DAY_1, "A", "M1", 0, "PCS"],
        [None, None, None, None, "PCS"],
    ]
    result = _read(_workbook(rows), CUTTING)
    assert len(result.rows) == 1
    assert result.blank_rows == 1
    assert not result.rejected


def test_text_date_is_rejected_instead_of_guessing_day_and_month() -> None:
    rows = [
        ["Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG"],
        [DAY_1, "A", "M1", 1],
        ["03/04/2030", "A", "M1", 2],
    ]
    result = _read(_workbook(rows), CUTTING)
    assert result.totals[0].counted == 1
    assert "không phải ngày" in result.rejected[0].reason


def test_total_rows_are_not_added_to_details() -> None:
    rows = [
        ["Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG"],
        [DAY_1, "A", "M1", 4],
        ["Tổng cộng", None, None, 4],
    ]
    result = _read(_workbook(rows), CUTTING)
    assert result.total_rows == 1
    assert result.totals[0].counted == 4


def test_formula_without_cached_value_is_rejected_not_zero() -> None:
    rows = [
        ["Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG"],
        [DAY_1, "A", "M1", "=1+1"],
    ]
    result = _read(_workbook(rows), CUTTING)
    assert not result.rows
    assert "chưa có giá trị" in result.rejected[0].reason


def test_stated_total_mismatch_is_reported() -> None:
    rows = [[None, None, None, 999]] + [
        ["Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG"],
        [DAY_1, "A", "M1", 4],
    ]
    assert _read(_workbook(rows), CUTTING).totals[0].matches_stated is False


def _spinning_rows() -> list[list[Any]]:
    return [
        [None, None, None, None, None, None, None, 15.0],
        [
            "NGÀY SX",
            "SỐ MÁY",
            "N/Đ",
            "CA SX",
            "MÃ SỢI",
            "SẢN LƯỢNG A",
            "Tên công nhân sản xuất",
            "SP Sợi",
        ],
        [DAY_1, 1, "N", "A", "S1", 10, "an", 5],
        [DAY_1, 1, "N", "A", "S1", 10, "binh", 5],
        [DAY_1, 2, "N", "A", "S1", 5, "chi", 5],
    ]


def test_output_repeated_per_worker_is_counted_once() -> None:
    result = _read(_workbook(_spinning_rows()), SPINNING)
    total = result.totals[0]
    assert total.all_rows == 25
    assert total.counted == 15
    assert result.repeated_rows == 1
    assert len(result.rows) == 3


def test_control_total_can_come_from_another_column() -> None:
    assert _read(_workbook(_spinning_rows()), SPINNING).totals[0].matches_stated is True


def test_same_output_on_another_day_is_not_a_repeat() -> None:
    rows = _spinning_rows()
    rows.append([DAY_2, 1, "N", "A", "S1", 10, "an", 10])
    assert _read(_workbook(rows), SPINNING).totals[0].counted == 25


def test_weaving_reports_both_quantity_fields() -> None:
    rows = [
        [None, None, None, None, None, 5, None, None, 9],
        [
            "NGÀY",
            "TÊN CN",
            "CA",
            "MÁY MỚI",
            "MÃ HÀNG",
            "THỰC HIỆN",
            "Số met đầu ca",
            "Số met cuối ca",
            "SỐ LƯỢNG NHẬP KHO MET",
        ],
        [DAY_1, "an", "A1", 1, "H1", 5, 0, 5, 9],
    ]
    result = _read(_workbook(rows), WEAVING)
    by_field = {total.field_key: total for total in result.totals}
    assert by_field["done"].matches_stated is True
    assert by_field["rolled"].matches_stated is True


def test_hidden_sheet_is_not_auto_selected() -> None:
    def add_hidden(workbook: Workbook) -> None:
        hidden = workbook.create_sheet("Ban cu")
        hidden.sheet_state = "hidden"
        for row in _cutting_rows():
            hidden.append(row)

    data = _workbook(_cutting_rows(), title="Bang tinh", prepare=add_hidden)
    assert suggest_sheet(read_previews(data), CUTTING) == "Bang tinh"


def _two_complete_sheets(first: str, second: str) -> list[SheetPreview]:
    rows = (
        ("Ngày SX", "CÔNG ĐOẠN SX", "MÃ CÔNG ĐOẠN", "S.LƯỢNG"),
        (DAY_1, "A", "M1", 1),
    )
    return [SheetPreview(first, False, rows), SheetPreview(second, False, rows)]


def test_known_sheet_name_settles_a_tie() -> None:
    assert (
        suggest_sheet(_two_complete_sheets("Khac", "NHAP LIEU"), CUTTING) == "NHAP LIEU"
    )


def test_tie_without_known_name_is_left_for_the_user() -> None:
    assert suggest_sheet(_two_complete_sheets("Khac", "Khac 2"), CUTTING) is None


def test_sheet_with_partial_match_is_not_suggested() -> None:
    preview = SheetPreview("Khac", False, (("Ngày SX", "S.LƯỢNG"), (DAY_1, 1)))
    assert suggest_sheet([preview], CUTTING) is None


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("1.BANG LUONG DET T10.xlsx", "det"),
        ("3.BANG LUONG CAT.xlsx", "cat"),
        ("khac.xlsx", None),
    ],
)
def test_template_is_suggested_from_file_name(
    file_name: str, expected: str | None
) -> None:
    found = suggest_template(file_name, (WEAVING, PRINTING, CUTTING, SPINNING))
    assert (found.key if found else None) == expected


@pytest.mark.parametrize("data", [b"", b"not a workbook"])
def test_corrupt_file_gives_a_readable_error(data: bytes) -> None:
    with pytest.raises(ExcelImportError):
        read_previews(data)
