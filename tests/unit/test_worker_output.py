"""Crediting output to employees, on synthetic workbooks only; no real data belongs here."""

from datetime import date, datetime
from io import BytesIO
from typing import Any

import pytest
from openpyxl import Workbook

from gui_san_luong.domain.production_templates import (
    BLOWING,
    JUMBO_SEWING,
    SEWING,
    SPINNING,
    TEMPLATES,
    WEAVING,
    DetailFields,
    FieldKind,
    FieldSpec,
    ReportTemplate,
    WorkerSlot,
)
from gui_san_luong.services.excel_import import (
    FieldTotal,
    ImportResult,
    MatchStatus,
    SheetPreview,
    analyze_sheet,
    read_rows,
    suggest_template,
)
from gui_san_luong.services.worker_output import (
    MAX_MESSAGE_CHARS,
    CreditedOutput,
    OutputDetail,
    WorkerOutput,
    available_days,
    build_messages,
    credit_workers,
    find_day_overlaps,
    find_name_conflicts,
    format_quantity,
    is_slot_mapped,
    list_employees,
    normalize_code,
    summarize_day,
)

DAY_1 = date(2030, 1, 1)
DAY_2 = date(2030, 1, 2)
NO_DETAIL = OutputDetail()

TWO_PEOPLE = ReportTemplate(
    key="test",
    label="Test",
    department="Bộ phận A",
    unit="cái",
    file_keywords=("test",),
    date_field="date",
    quantity_field="quantity",
    group_by=("date",),
    process_field="process",
    worker_slots=(
        WorkerSlot(
            role="Chính",
            code_field="code_1",
            quantity_field="share_1",
            name_fields=("full_1", "nick_1"),
        ),
        WorkerSlot(
            role="Phụ",
            code_field="code_2",
            quantity_field="share_2",
            name_fields=("nick_2",),
        ),
    ),
    fields=(
        FieldSpec(
            key="date",
            label="Ngày",
            kind=FieldKind.DATE,
            aliases=("ngay",),
            required=True,
        ),
        FieldSpec(
            key="quantity",
            label="SL",
            kind=FieldKind.NUMBER,
            aliases=("sl",),
            required=True,
        ),
    ),
)
FULL_MAPPING = {
    "code_1": 0,
    "share_1": 1,
    "full_1": 2,
    "nick_1": 3,
    "code_2": 4,
    "share_2": 5,
    "nick_2": 6,
}


def _row(**values: Any) -> dict[str, Any]:
    return {"_row": 7, "date": DAY_1, "_counted": True, "quantity": 100.0, **values}


def _credit(*rows: dict[str, Any], source: str = "a.xlsx") -> CreditedOutput:
    result = ImportResult(rows=list(rows))
    return credit_workers(result, TWO_PEOPLE, FULL_MAPPING, source)


def _weaving_totals() -> list[FieldTotal]:
    header = ["NGÀY", "CA", "MÁY", "MÃ HÀNG", "THỰC HIỆN", "HỆ SỐ CHIA LƯƠNG", None]
    rows = [header, [datetime(2030, 1, 1), "A1", 1, "H1", 100, 1, 0]]
    preview = SheetPreview("chi tiet", False, tuple(map(tuple, rows)))
    layout = analyze_sheet(preview, WEAVING)
    mapping = {
        key: match.column
        for key, match in layout.matches.items()
        if match.column is not None
    }
    assert "ratio_main" in mapping
    return read_rows(_workbook(rows), "Sheet", WEAVING, mapping, 2).totals


def _output(
    code: str = "10",
    name: str = "Nguyễn Văn A",
    department: str = "Dệt",
    role: str = "Thợ chính",
    day: date = DAY_1,
    quantity: float = 100.0,
    unit: str | None = "mét",
    source: str = "det.xlsx",
    detail: OutputDetail = NO_DETAIL,
    base: float | None = None,
) -> WorkerOutput:
    return WorkerOutput(
        employee_code=code,
        employee_name=name,
        source=source,
        department=department,
        role=role,
        process=None,
        day=day,
        quantity=quantity,
        unit=unit,
        base_quantity=quantity if base is None else base,
        detail=detail,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12, "12"),
        (12.0, "12"),
        (" 12 ", "12"),
        ("HV1", "HV1"),
        (0, None),
        ("0", None),
        ("", None),
        (None, None),
        (True, None),
        (12.5, None),
    ],
)
def test_normalize_code(value: object, expected: str | None) -> None:
    assert normalize_code(value) == expected


def test_each_person_in_a_row_gets_their_own_share() -> None:
    credited = _credit(
        _row(code_1=10, share_1=60.0, nick_1="an", code_2=20, share_2=40.0, nick_2="bo")
    )
    assert [(o.employee_code, o.role, o.quantity) for o in credited.outputs] == [
        ("10", "Chính", 60.0),
        ("20", "Phụ", 40.0),
    ]
    assert not credited.problems


@pytest.mark.parametrize("code", ["cty", "CTY", " Cty "])
def test_company_share_is_sent_to_nobody_but_the_worker_beside_it_is(
    code: str,
) -> None:
    credited = _credit(
        _row(
            code_1=10,
            share_1=60.0,
            nick_1="an",
            code_2=code,
            share_2=40.0,
            nick_2="CTY",
        )
    )
    assert [o.employee_code for o in credited.outputs] == ["10"]
    assert not credited.problems
    share = credited.company_shares[0]
    assert (share.excel_row, share.day, share.quantity) == (7, DAY_1, 40.0)


def test_company_name_without_code_is_a_company_share_not_a_problem() -> None:
    credited = _credit(_row(share_1=40.0, nick_1="CTY"))
    assert not credited.outputs
    assert not credited.problems
    assert credited.company_shares[0].quantity == 40.0


def test_real_code_with_company_written_as_name_is_still_sent() -> None:
    credited = _credit(_row(code_1=10, share_1=40.0, nick_1="CTY"))
    assert [o.employee_code for o in credited.outputs] == ["10"]
    assert not credited.company_shares


def test_company_share_of_zero_is_not_listed() -> None:
    assert not _credit(_row(code_1="cty", share_1=0.0, nick_1="CTY")).company_shares


def test_company_share_follows_the_coefficient() -> None:
    credited = _credit_shared(
        code_1=10, nick_1="an", ratio_1=0.85, code_2="cty", nick_2="CTY", ratio_2=0.15
    )
    assert credited.company_shares[0].quantity == 150.0


def test_full_name_is_preferred_over_nickname() -> None:
    credited = _credit(_row(code_1=10, share_1=1.0, full_1="Trần Văn An", nick_1="an"))
    assert credited.outputs[0].employee_name == "Trần Văn An"


def test_zero_share_credits_nobody() -> None:
    credited = _credit(_row(code_1=10, share_1=0.0, nick_1="an"))
    assert not credited.outputs
    assert not credited.problems


def test_output_without_employee_code_is_reported_not_dropped() -> None:
    credited = _credit(_row(code_1=None, share_1=50.0, nick_1="an"))
    assert not credited.outputs
    assert credited.problems[0].day == DAY_1
    assert credited.problems[0].excel_row == 7
    assert "mã số" in credited.problems[0].reason


def test_blank_share_is_reported_not_treated_as_zero() -> None:
    credited = _credit(_row(code_1=10, share_1=None, nick_1="an"))
    assert not credited.outputs
    assert "thiếu số lượng" in credited.problems[0].reason


SHARED_MACHINE = TWO_PEOPLE.model_copy(
    update={
        "worker_slots": (
            WorkerSlot(
                role="Chính",
                code_field="code_1",
                quantity_field="quantity",
                name_fields=("nick_1",),
                ratio_field="ratio_1",
            ),
            WorkerSlot(
                role="Phụ",
                code_field="code_2",
                quantity_field="quantity",
                name_fields=("nick_2",),
                ratio_field="ratio_2",
            ),
        )
    }
)
SHARED_MAPPING = {
    "quantity": 0,
    "code_1": 1,
    "nick_1": 2,
    "ratio_1": 3,
    "code_2": 4,
    "nick_2": 5,
    "ratio_2": 6,
}


def _credit_shared(**values: Any) -> CreditedOutput:
    result = ImportResult(rows=[_row(quantity=1000.0, **values)])
    return credit_workers(result, SHARED_MACHINE, SHARED_MAPPING, "a.xlsx")


def test_shared_machine_output_is_split_by_coefficient() -> None:
    credited = _credit_shared(
        code_1=10, nick_1="an", ratio_1=0.85, code_2=20, nick_2="bo", ratio_2=0.15
    )
    assert [(o.employee_code, o.quantity) for o in credited.outputs] == [
        ("10", 850.0),
        ("20", 150.0),
    ]
    assert not credited.problems


def test_person_with_zero_coefficient_gets_nothing_even_without_identity() -> None:
    credited = _credit_shared(code_1=10, nick_1="an", ratio_1=1, ratio_2=0)
    assert [o.quantity for o in credited.outputs] == [1000.0]
    assert not credited.problems


def test_missing_coefficient_is_reported_not_treated_as_a_whole_share() -> None:
    credited = _credit_shared(code_1=10, nick_1="an", ratio_1=None, ratio_2=0)
    assert not credited.outputs
    assert "thiếu hệ số" in credited.problems[0].reason


def test_row_naming_no_worker_is_skipped_without_a_problem() -> None:
    credited = _credit_shared(code_1=10, nick_1="an", ratio_1=0.85, ratio_2=0.15)
    assert [o.quantity for o in credited.outputs] == [850.0]
    assert not credited.problems
    empty = _credit(_row(share_1=50.0))
    assert not empty.outputs
    assert not empty.problems
    assert not empty.company_shares


def test_slot_needing_a_coefficient_column_is_skipped_without_it() -> None:
    mapping = {k: v for k, v in SHARED_MAPPING.items() if k != "ratio_2"}
    assert not is_slot_mapped(SHARED_MACHINE.worker_slots[1], mapping)


def test_coefficient_gets_no_control_total() -> None:
    assert not any(total.field_key.startswith("ratio") for total in _weaving_totals())


def test_list_employees_merges_names_and_departments_by_code() -> None:
    employees = list_employees(
        [
            _output(code="10", name="an", department="May"),
            _output(code="10", name="Trần Văn An", department="Dệt"),
        ]
    )
    assert [(e.employee_code, e.employee_name, e.departments) for e in employees] == [
        ("10", "Trần Văn An", ("Dệt", "May"))
    ]


def test_slot_without_mapped_columns_is_skipped() -> None:
    mapping = {key: value for key, value in FULL_MAPPING.items() if key != "code_2"}
    result = ImportResult(
        rows=[_row(code_1=10, share_1=5.0, nick_1="an", share_2=5.0, nick_2="bo")]
    )
    credited = credit_workers(result, TWO_PEOPLE, mapping, "a.xlsx")
    assert [o.employee_code for o in credited.outputs] == ["10"]


def test_summary_follows_a_person_across_departments_by_code() -> None:
    outputs = [
        _output(code="10", name="an", department="Dệt", day=DAY_1),
        _output(
            code="10",
            name="Trần Văn An",
            department="May",
            role="May",
            day=DAY_2,
            unit="cái",
        ),
    ]
    employee = summarize_day(outputs, DAY_2)[0]
    assert employee.employee_name == "Trần Văn An"
    assert employee.was_transferred
    assert employee.period_departments == ("Dệt", "May")
    assert [line.department for line in employee.lines] == ["May"]


def test_summary_shows_only_the_selected_day() -> None:
    outputs = [_output(code="10", day=DAY_1), _output(code="20", day=DAY_2)]
    assert [e.employee_code for e in summarize_day(outputs, DAY_1)] == ["10"]
    assert available_days(outputs) == [DAY_1, DAY_2]


def test_same_nickname_with_different_codes_stays_two_people() -> None:
    outputs = [_output(code="10", name="chau"), _output(code="20", name="chau")]
    assert len(summarize_day(outputs, DAY_1)) == 2


def test_summary_adds_within_a_role_but_not_across_units() -> None:
    outputs = [
        _output(quantity=100.0),
        _output(quantity=50.0),
        _output(department="May", role="May", quantity=7.0, unit="cái"),
    ]
    lines = summarize_day(outputs, DAY_1)[0].lines
    assert [(line.department, line.quantity, line.unit) for line in lines] == [
        ("Dệt", 150.0, "mét"),
        ("May", 7.0, "cái"),
    ]


def test_overlap_is_found_only_for_the_day_both_files_cover() -> None:
    outputs = [
        _output(day=DAY_1, source="a.xlsx"),
        _output(day=DAY_1, source="b.xlsx"),
        _output(day=DAY_2, source="a.xlsx"),
    ]
    assert find_day_overlaps(outputs, DAY_1) == {"Dệt": ["a.xlsx", "b.xlsx"]}
    assert find_day_overlaps(outputs, DAY_2) == {}


def test_name_conflict_compares_full_names_and_ignores_nicknames() -> None:
    outputs = [
        _output(code="10", name="an"),
        _output(code="10", name="Trần Văn An"),
        _output(code="10", name="TRẦN VĂN AN"),
        _output(code="20", name="Lê Thị Bình"),
        _output(code="20", name="Lê Thị Hoa"),
    ]
    assert find_name_conflicts(outputs) == {"20": ["Lê Thị Bình", "Lê Thị Hoa"]}


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [(1234.5, "1.234,5"), (1000.0, "1.000"), (0.25, "0,25"), (12.0, "12")],
)
def test_format_quantity_uses_vietnamese_style(quantity: float, expected: str) -> None:
    assert format_quantity(quantity) == expected


def test_message_lists_each_item_machine_and_shift_under_a_total() -> None:
    employee = summarize_day(
        [
            _output(
                code="10",
                name="Trần Văn An",
                quantity=1000.0,
                detail=OutputDetail("H1", "103", "B1"),
            ),
            _output(
                code="10",
                name="Trần Văn An",
                quantity=234.5,
                base=469.0,
                detail=OutputDetail("H2", "102", "A1", overtime=True),
            ),
        ],
        DAY_1,
    )[0]
    expected = (
        "Sản lượng ngày 01/01/2030\n"
        "Trần Văn An (MS 10)\n"
        "\n"
        "Dệt (Thợ chính): 1.234,5 mét\n"
        "- Ca A1, máy 102, H2: 234,5 mét (SL chính 469, hệ số 0,5), tăng ca\n"
        "- Ca B1, máy 103, H1: 1.000 mét (SL chính 1.000, hệ số 1)"
    )
    assert build_messages(employee) == [expected]


def test_each_excel_row_stays_its_own_line_even_when_it_looks_identical() -> None:
    detail = OutputDetail("H1", "103", "B1")
    employee = summarize_day(
        [
            _output(quantity=259.0, detail=detail),
            _output(quantity=49.0, detail=detail),
        ],
        DAY_1,
    )[0]
    line = employee.lines[0]
    assert line.quantity == 308.0
    assert [item.quantity for item in line.details] == [259.0, 49.0]
    assert build_messages(employee)[0].endswith(
        "Dệt (Thợ chính): 308 mét\n"
        "- Ca B1, máy 103, H1: 259 mét (SL chính 259, hệ số 1)\n"
        "- Ca B1, máy 103, H1: 49 mét (SL chính 49, hệ số 1)"
    )


def test_coefficient_is_rounded_for_display_only() -> None:
    employee = summarize_day([_output(quantity=1.0, base=3.0)], DAY_1)[0]
    assert build_messages(employee)[0].endswith("1 mét (SL chính 3, hệ số 0,333)")


def test_credit_keeps_the_rows_output_next_to_the_persons_share() -> None:
    result = ImportResult(
        rows=[
            _row(
                quantity=1000.0,
                code_1=10,
                nick_1="an",
                ratio_1=0.85,
                code_2=20,
                nick_2="bo",
                ratio_2=0.15,
            )
        ]
    )
    outputs = credit_workers(result, SHARED_MACHINE, SHARED_MAPPING, "a.xlsx").outputs
    assert [(o.quantity, o.base_quantity) for o in outputs] == [
        (850.0, 1000.0),
        (150.0, 1000.0),
    ]


def test_same_step_on_different_products_stays_on_separate_lines() -> None:
    employee = summarize_day(
        [
            _output(quantity=30.0, detail=OutputDetail("JBO-1", step="May quai")),
            _output(quantity=40.0, detail=OutputDetail("JBO-2", step="May quai")),
        ],
        DAY_1,
    )[0]
    message = build_messages(employee)[0]
    assert "- JBO-1, May quai: 30 mét" in message
    assert "- JBO-2, May quai: 40 mét" in message


def test_overtime_is_kept_apart_from_regular_hours() -> None:
    employee = summarize_day(
        [
            _output(quantity=10.0, detail=OutputDetail("H1")),
            _output(quantity=5.0, detail=OutputDetail("H1", overtime=True)),
        ],
        DAY_1,
    )[0]
    assert [line.quantity for line in employee.lines[0].details] == [10.0, 5.0]
    assert employee.lines[0].quantity == 15.0


def test_message_omits_what_the_report_does_not_state() -> None:
    employee = summarize_day([_output(unit=None, quantity=5.0)], DAY_1)[0]
    assert build_messages(employee)[0].endswith(
        "Dệt (Thợ chính): 5\n- 5 (SL chính 5, hệ số 1)"
    )


def test_long_message_is_split_between_lines_within_the_limit() -> None:
    outputs = [
        _output(quantity=float(i + 1), detail=OutputDetail(f"MA{i:03d}" + "X" * 60))
        for i in range(120)
    ]
    messages = build_messages(summarize_day(outputs, DAY_1)[0])
    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_CHARS for message in messages)
    assert messages[0].startswith(f"Sản lượng ngày 01/01/2030 (phần 1/{len(messages)})")
    assert all("Nguyễn Văn A (MS 10)" in message for message in messages)
    sent = "\n".join(messages)
    assert all(f"MA{i:03d}" in sent for i in range(120))


def test_very_long_item_text_is_shortened_but_keeps_its_quantity() -> None:
    employee = summarize_day(
        [_output(quantity=7.0, detail=OutputDetail("X" * 5000))], DAY_1
    )[0]
    messages = build_messages(employee)
    assert len(messages) == 1
    assert messages[0].endswith("...: 7 mét (SL chính 7, hệ số 1)")


def test_detail_is_read_from_the_template_fields_with_machine_as_text() -> None:
    template = TWO_PEOPLE.model_copy(
        update={
            "detail": DetailFields(
                item_fields=("item_a", "item_b"),
                machine_fields=("device", "machine"),
                shift_fields=("day_night", "shift"),
                overtime_field="overtime",
            )
        }
    )
    result = ImportResult(
        rows=[
            _row(
                code_1=10,
                share_1=1.0,
                nick_1="an",
                item_a=None,
                item_b="H2",
                device="LO",
                machine=3.0,
                day_night="N",
                shift="A",
                overtime="x",
            )
        ]
    )
    detail = credit_workers(result, template, FULL_MAPPING, "a.xlsx").outputs[0].detail
    assert detail == OutputDetail("H2", "LO 3", "N A", overtime=True)


def test_jumbo_file_is_not_taken_for_regular_sewing() -> None:
    assert suggest_template("5.BANG LUONG MAY T10-2026.xlsx", TEMPLATES) is SEWING
    assert (
        suggest_template("6.BANG LUONG MAY JUMBO T10-2026.xlsx", TEMPLATES)
        is JUMBO_SEWING
    )
    assert suggest_template("7.BANG LUONG O T10-2026.xlsx", TEMPLATES) is BLOWING


def _workbook(rows: list[list[Any]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _blowing_rows(shift_by: int = 0) -> list[list[Any]]:
    """A report whose worker columns have repeated headers under merged titles."""
    pad: list[Any] = [None] * shift_by
    return [
        pad + [None, None, 90] + [None] * 6 + [60, 30, 0],
        pad
        + ["NGÀY", "CA", "KG", "TÊN", None, None]
        + ["MÃ SỐ", "MÃ SỐ", "MÃ SỐ", "SẢN PHẨM", None, None],
        pad + [None] * 3 + [None, None, None, None, None, None, "CN01", "CN02", "CN02"],
        pad
        + [datetime(2030, 1, 1), "N", 90, "an", "bo", None, 10, 20, None, 60, 30, 0],
        pad + [None] * 12,
        pad + ["Kế toán"] + [None] * 11,
    ]


def _read_blowing(
    rows: list[list[Any]], data_start_row: int = 4
) -> tuple[ImportResult, dict[str, int]]:
    preview = SheetPreview("CHI TIET", False, tuple(map(tuple, rows)))
    layout = analyze_sheet(preview, BLOWING)
    assert layout.data_start_row == data_start_row
    mapping = {
        key: match.column
        for key, match in layout.matches.items()
        if match.column is not None
    }
    assert all(match.status is MatchStatus.AUTO for match in layout.matches.values())
    data = _workbook(rows)
    return read_rows(data, "Sheet", BLOWING, mapping, layout.data_start_row), mapping


def test_repeated_headers_are_found_by_distance_from_a_unique_header() -> None:
    result, mapping = _read_blowing(_blowing_rows())
    credited = credit_workers(result, BLOWING, mapping, "o.xlsx")
    assert [(o.employee_code, o.quantity) for o in credited.outputs] == [
        ("10", 60.0),
        ("20", 30.0),
    ]


def test_anchored_columns_still_work_after_columns_shift() -> None:
    result, mapping = _read_blowing(_blowing_rows(shift_by=2))
    credited = credit_workers(result, BLOWING, mapping, "o.xlsx")
    assert [(o.employee_code, o.quantity) for o in credited.outputs] == [
        ("10", 60.0),
        ("20", 30.0),
    ]


def test_signature_line_without_date_or_quantity_is_not_rejected() -> None:
    result, _ = _read_blowing(_blowing_rows())
    assert not result.rejected
    assert result.blank_rows == 2


def test_control_total_is_found_despite_a_helper_number_in_the_same_column() -> None:
    rows = _blowing_rows()
    rows.insert(1, [None] * 9 + [1.03])
    result, _ = _read_blowing(rows, data_start_row=5)
    share = next(total for total in result.totals if total.field_key == "share_1")
    assert share.matches_stated is True


def test_broken_code_keeps_the_row_and_reports_the_person() -> None:
    rows = _blowing_rows()
    rows[3][6] = "#N/A"
    result, mapping = _read_blowing(rows)
    assert not result.rejected
    credited = credit_workers(result, BLOWING, mapping, "o.xlsx")
    assert [o.employee_code for o in credited.outputs] == ["20"]
    assert "mã số" in credited.problems[0].reason


def test_share_of_a_repeated_output_counts_for_every_person() -> None:
    rows = [
        [None] * 5 + [100, None, None, None, 100],
        ["NGÀY SX", "SỐ MÁY", "N/Đ", "CA SX", "MÃ SỢI", "SẢN LƯỢNG"]
        + ["TÊN CÔNG NHÂN SẢN XUẤT", "MÃ SỐ", "HỌ VÀ TÊN", "SP SỢI"],
        [datetime(2030, 1, 1), 1, "N", "A", "S1", 100, "an", 10, "Trần An", 50],
        [datetime(2030, 1, 1), 1, "N", "A", "S1", 100, "bo", 20, "Lê Bo", 50],
    ]
    preview = SheetPreview("CHI TIET", False, tuple(map(tuple, rows)))
    layout = analyze_sheet(preview, SPINNING)
    assert layout.data_start_row == 3
    mapping = {
        key: match.column
        for key, match in layout.matches.items()
        if match.column is not None
    }
    result = read_rows(_workbook(rows), "Sheet", SPINNING, mapping, 3)
    totals = {total.field_key: total for total in result.totals}
    assert totals["output_a"].counted == 100
    assert totals["share"].counted == 100
    credited = credit_workers(result, SPINNING, mapping, "soi.xlsx")
    assert sum(o.quantity for o in credited.outputs) == 100


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda template: template.key)
def test_template_only_refers_to_fields_it_declares(template: ReportTemplate) -> None:
    declared = {spec.key for spec in template.fields}
    detail = template.detail
    referenced = {
        *detail.item_fields,
        *detail.step_fields,
        *detail.machine_fields,
        *detail.shift_fields,
    }
    if detail.overtime_field:
        referenced.add(detail.overtime_field)
    for slot in template.worker_slots:
        referenced |= {slot.code_field, slot.quantity_field, *slot.name_fields}
        referenced |= {key for key in (slot.role_field, slot.ratio_field) if key}
    assert referenced <= declared
