"""Which fields each production report needs and how their column headers may be written.

Aliases are normalized header phrases (lowercase, no accents, punctuation as spaces),
because columns may move between files while their header text stays recognizable.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FieldKind(StrEnum):
    DATE = "date"
    NUMBER = "number"
    TEXT = "text"
    # Machine numbers and similar identifiers may be stored as either numbers or text.
    CODE = "code"


class FieldSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    kind: FieldKind
    aliases: tuple[str, ...] = ()
    required: bool = False
    # Some reports repeat a header ("cắt", "CN02") per block, or leave it blank under a merged
    # title. Such a column is found by its distance from the one header that is unique.
    anchor_aliases: tuple[str, ...] = ()
    anchor_offset: int = 0


class WorkerSlot(BaseModel):
    """One person's share of a row; a row may credit several people in different roles."""

    model_config = ConfigDict(frozen=True)

    role: str
    code_field: str
    quantity_field: str
    # In order of preference, because not every report holds a full name.
    name_fields: tuple[str, ...]
    # The column stating the person's position, when the report has one.
    role_field: str | None = None
    # Set when the report states a person's coefficient but not their resulting share, so the
    # share is the quantity field times this field.
    ratio_field: str | None = None


class DetailFields(BaseModel):
    """Which fields describe a line of output, matching what each payslip shows."""

    model_config = ConfigDict(frozen=True)

    # In order of preference.
    item_fields: tuple[str, ...] = ()
    # Shown beside the item, because one step name recurs across different products.
    step_fields: tuple[str, ...] = ()
    # Joined, because a machine number may only be unique within its device.
    machine_fields: tuple[str, ...] = ()
    # Joined, because some reports split day/night and shift into two columns.
    shift_fields: tuple[str, ...] = ()
    overtime_field: str | None = None
    # The part of the row's output made in overtime, when the report states it apart.
    overtime_quantity_field: str | None = None


class ReportTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    department: str
    # None when the report does not state a unit; a unit is never assumed for it.
    unit: str | None = None
    file_keywords: tuple[str, ...]
    file_excludes: tuple[str, ...] = ()
    # Normalized sheet names that settle a tie between sheets with matching columns.
    sheet_hints: tuple[str, ...] = ()
    fields: tuple[FieldSpec, ...]
    date_field: str
    quantity_field: str
    group_by: tuple[str, ...]
    # Rows sharing these values describe one output repeated per worker; count it once.
    duplicate_key: tuple[str, ...] = ()
    # A number field's control total may sit above a different column than the field itself.
    control_aliases: dict[str, tuple[str, ...]] = {}
    worker_slots: tuple[WorkerSlot, ...] = ()
    # Set when output of different processes in one report must not be added together.
    process_field: str | None = None
    detail: DetailFields = DetailFields()

    def field(self, key: str) -> FieldSpec:
        return next(spec for spec in self.fields if spec.key == key)


def _code(key: str, label: str, *aliases: str) -> FieldSpec:
    return FieldSpec(key=key, label=label, kind=FieldKind.CODE, aliases=aliases)


def _machine() -> FieldSpec:
    return _code("machine", "Máy", "may hd")


def _shift() -> FieldSpec:
    return _code("shift", "Ca", "ca")


def _overtime() -> FieldSpec:
    return _code("overtime", "Tăng ca", "tang ca x", "tang ca")


def _anchored(
    key: str, label: str, kind: FieldKind, anchor: str, offset: int = 0
) -> FieldSpec:
    return FieldSpec(
        key=key,
        label=label,
        kind=kind,
        anchor_aliases=(anchor,),
        anchor_offset=offset,
    )


WEAVING = ReportTemplate(
    key="det",
    label="1. Dệt",
    department="Dệt",
    unit="mét",
    file_keywords=("det",),
    # "chi tiet" comes first: only it holds employee codes, needed to follow a person across files.
    sheet_hints=("chi tiet", "chi tiet nhap vai", "chi tiet vai hang"),
    date_field="date",
    quantity_field="done",
    group_by=("date",),
    # Numbers above these two columns are not totals of them.
    control_aliases={"meter_start": (), "meter_end": ()},
    detail=DetailFields(
        item_fields=("item_for_pay", "item"),
        machine_fields=("machine",),
        shift_fields=("shift",),
        overtime_field="overtime",
    ),
    worker_slots=(
        WorkerSlot(
            role="Thợ chính",
            code_field="code_main",
            quantity_field="done",
            name_fields=("name_main", "worker"),
            ratio_field="ratio_main",
        ),
        WorkerSlot(
            role="Thợ phụ",
            code_field="code_helper",
            quantity_field="done",
            name_fields=("name_helper",),
            ratio_field="ratio_helper",
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
            key="worker", label="Tên CN", kind=FieldKind.TEXT, aliases=("ten cn",)
        ),
        FieldSpec(
            key="machine",
            label="Máy",
            kind=FieldKind.CODE,
            aliases=("may moi", "may"),
            required=True,
        ),
        # "CA" also heads a helper column far to the right, so read it beside the date.
        FieldSpec(
            key="shift",
            label="Ca",
            kind=FieldKind.TEXT,
            anchor_aliases=("ngay",),
            anchor_offset=1,
            required=True,
        ),
        FieldSpec(
            key="item",
            label="Mã hàng",
            kind=FieldKind.TEXT,
            aliases=("ma hang moi", "ma hang"),
            required=True,
        ),
        FieldSpec(
            key="done",
            label="Thực hiện",
            kind=FieldKind.NUMBER,
            aliases=("thuc hien",),
            required=True,
        ),
        FieldSpec(
            key="meter_start",
            label="Số mét đầu ca",
            kind=FieldKind.NUMBER,
            aliases=("so met dau ca",),
        ),
        FieldSpec(
            key="meter_end",
            label="Số mét cuối ca",
            kind=FieldKind.NUMBER,
            aliases=("so met cuoi ca",),
        ),
        FieldSpec(
            key="rolled",
            label="Mét xuống cuộn",
            kind=FieldKind.NUMBER,
            aliases=("so luong nhap kho met", "so met xuong cuon", "met xuong cuon"),
        ),
        # The code printed on payslips, in the unnamed or repeated column after the order number.
        _anchored("item_for_pay", "Mã hàng tính lương", FieldKind.CODE, "lsx", 1),
        _overtime(),
        _anchored("code_main", "Mã số thợ chính", FieldKind.CODE, "ma so cn"),
        _anchored("code_helper", "Mã số thợ phụ", FieldKind.CODE, "ma so cn", 1),
        _anchored("name_main", "Họ tên thợ chính", FieldKind.TEXT, "ho ten"),
        _anchored("name_helper", "Họ tên thợ phụ", FieldKind.TEXT, "ho ten", 1),
        _anchored(
            "ratio_main", "Hệ số thợ chính", FieldKind.NUMBER, "he so chia luong"
        ),
        _anchored(
            "ratio_helper", "Hệ số thợ phụ", FieldKind.NUMBER, "he so chia luong", 1
        ),
    ),
)

PRINTING = ReportTemplate(
    key="in",
    label="2. In",
    department="In",
    file_keywords=("in",),
    sheet_hints=("chi tiet",),
    date_field="date",
    quantity_field="quantity",
    group_by=("date", "process"),
    process_field="process",
    detail=DetailFields(
        item_fields=("process_code",),
        machine_fields=("machine",),
        shift_fields=("shift",),
        overtime_field="overtime",
    ),
    worker_slots=(
        WorkerSlot(
            role="Đứng máy",
            code_field="code_operator",
            quantity_field="share_operator",
            name_fields=("operator",),
        ),
        WorkerSlot(
            role="Cột bao",
            code_field="code_bag_tier",
            quantity_field="share_bag_tier",
            name_fields=("bag_tier",),
        ),
        WorkerSlot(
            role="Xếp bao",
            code_field="code_bag_stack",
            quantity_field="share_bag_stack",
            name_fields=("bag_stack",),
        ),
        WorkerSlot(
            role="Phụ in",
            code_field="code_assistant",
            quantity_field="share_assistant",
            name_fields=("assistant",),
        ),
    ),
    fields=(
        FieldSpec(
            key="process",
            label="Công đoạn sản xuất",
            kind=FieldKind.TEXT,
            aliases=("cong doan sx", "cong doan san xuat"),
            required=True,
        ),
        FieldSpec(
            key="process_code",
            label="Mã công đoạn",
            kind=FieldKind.TEXT,
            aliases=("ma cong doan",),
            required=True,
        ),
        FieldSpec(
            key="date",
            label="Ngày sản xuất",
            kind=FieldKind.DATE,
            aliases=("ngay sx", "ngay san xuat"),
            required=True,
        ),
        FieldSpec(
            key="quantity",
            label="Số lượng",
            kind=FieldKind.NUMBER,
            aliases=("s luong", "so luong"),
            required=True,
        ),
        FieldSpec(
            key="operator", label="Đứng máy", kind=FieldKind.TEXT, aliases=("dung may",)
        ),
        FieldSpec(
            key="bag_tier", label="Cột bao", kind=FieldKind.TEXT, aliases=("cot bao",)
        ),
        FieldSpec(
            key="bag_stack", label="Xếp bao", kind=FieldKind.TEXT, aliases=("xep bao",)
        ),
        FieldSpec(
            key="assistant", label="Phụ in", kind=FieldKind.TEXT, aliases=("phu in",)
        ),
        _machine(),
        _shift(),
        _overtime(),
        _code("code_operator", "Mã số đứng máy", "ma so cn1"),
        _code("code_bag_tier", "Mã số cột bao", "ma so cn2"),
        _code("code_bag_stack", "Mã số xếp bao", "ma so cn3"),
        _code("code_assistant", "Mã số phụ in", "ma so cn4"),
        _anchored(
            "share_operator", "SP đứng máy", FieldKind.NUMBER, "san pham chia luong"
        ),
        _anchored(
            "share_bag_tier", "SP cột bao", FieldKind.NUMBER, "san pham chia luong", 1
        ),
        _anchored(
            "share_bag_stack", "SP xếp bao", FieldKind.NUMBER, "san pham chia luong", 2
        ),
        _anchored(
            "share_assistant", "SP phụ in", FieldKind.NUMBER, "san pham chia luong", 3
        ),
    ),
)

CUTTING = ReportTemplate(
    key="cat",
    label="3. In - Cắt",
    department="Cắt",
    unit="cái",
    file_keywords=("cat",),
    sheet_hints=("nhap lieu",),
    date_field="date",
    quantity_field="quantity",
    group_by=("date", "process"),
    detail=DetailFields(
        item_fields=("process_code",),
        machine_fields=("machine",),
        shift_fields=("shift",),
        overtime_field="overtime",
    ),
    worker_slots=(
        WorkerSlot(
            role="NV01",
            code_field="code_1",
            quantity_field="share_1",
            name_fields=("name_1", "worker_1"),
        ),
        WorkerSlot(
            role="NV02",
            code_field="code_2",
            quantity_field="share_2",
            name_fields=("worker_2",),
        ),
    ),
    fields=(
        FieldSpec(
            key="date",
            label="Ngày sản xuất",
            kind=FieldKind.DATE,
            aliases=("ngay sx", "ngay san xuat"),
            required=True,
        ),
        FieldSpec(
            key="process",
            label="Công đoạn sản xuất",
            kind=FieldKind.TEXT,
            aliases=("cong doan sx", "cong doan san xuat"),
            required=True,
        ),
        FieldSpec(
            key="process_code",
            label="Mã công đoạn",
            kind=FieldKind.TEXT,
            aliases=("ma cong doan",),
            required=True,
        ),
        FieldSpec(
            key="worker_1",
            label="Tên CN NV01",
            kind=FieldKind.TEXT,
            aliases=("ten cn nv01",),
        ),
        FieldSpec(
            key="worker_2",
            label="Tên CN NV02",
            kind=FieldKind.TEXT,
            aliases=("ten cn nv02",),
        ),
        FieldSpec(
            key="quantity",
            label="Số lượng",
            kind=FieldKind.NUMBER,
            aliases=("s luong", "so luong"),
            required=True,
        ),
        FieldSpec(key="unit", label="Đơn vị", kind=FieldKind.TEXT, aliases=("don vi",)),
        _machine(),
        _shift(),
        _overtime(),
        _anchored("code_1", "Mã số NV01", FieldKind.CODE, "ten cn nv01", 3),
        _anchored("code_2", "Mã số NV02", FieldKind.CODE, "ten cn nv01", 4),
        _anchored("share_1", "SP NV01", FieldKind.NUMBER, "s luong don vi cai"),
        _anchored("share_2", "SP NV02", FieldKind.NUMBER, "s luong don vi cai", 1),
        # The unheaded column after the shares looks NV01's full name up from "Mã số 01".
        # Read as a code so a failed lookup (#N/A) falls back to the nickname instead of
        # rejecting the row, the same as a broken code.
        _anchored("name_1", "Họ tên NV01", FieldKind.CODE, "s luong don vi cai", 2),
    ),
)

SPINNING = ReportTemplate(
    key="soi",
    label="4. Sợi",
    department="Sợi",
    unit="kg",
    file_keywords=("soi",),
    sheet_hints=("chi tiet", "nhap lieu"),
    date_field="date",
    quantity_field="output_a",
    group_by=("date",),
    # Each machine shift is listed once per worker with the same output.
    duplicate_key=("date", "machine", "day_night", "shift", "yarn_code", "output_a"),
    control_aliases={"output_a": ("sp soi",)},
    detail=DetailFields(
        item_fields=("yarn_name", "yarn_code"),
        machine_fields=("device", "machine"),
        shift_fields=("day_night", "shift"),
        overtime_field="overtime",
    ),
    worker_slots=(
        WorkerSlot(
            role="Công nhân",
            code_field="code",
            quantity_field="share",
            name_fields=("full_name", "worker"),
        ),
    ),
    fields=(
        FieldSpec(
            key="date",
            label="Ngày sản xuất",
            kind=FieldKind.DATE,
            aliases=("ngay sx", "ngay san xuat"),
            required=True,
        ),
        FieldSpec(
            key="machine",
            label="Số máy",
            kind=FieldKind.CODE,
            aliases=("so may",),
            required=True,
        ),
        FieldSpec(
            key="day_night",
            label="Ngày/Đêm (N/Đ)",
            kind=FieldKind.TEXT,
            aliases=("n d",),
            required=True,
        ),
        FieldSpec(
            key="shift",
            label="Ca sản xuất",
            kind=FieldKind.TEXT,
            aliases=("ca sx",),
            required=True,
        ),
        FieldSpec(
            key="yarn_code",
            label="Mã sợi",
            kind=FieldKind.TEXT,
            aliases=("ma soi",),
            required=True,
        ),
        FieldSpec(
            key="output_a",
            label="Sản lượng A (kg)",
            kind=FieldKind.NUMBER,
            aliases=("san luong a", "san luong"),
            required=True,
        ),
        FieldSpec(
            key="worker",
            label="Tên công nhân",
            kind=FieldKind.TEXT,
            aliases=("ten cong nhan san xuat", "ten cong nhan", "ten cn"),
        ),
        _code("yarn_name", "Tên sợi", "ten soi"),
        _code("device", "Thiết bị", "ten thiet bi"),
        _overtime(),
        _anchored("code", "Mã số", FieldKind.CODE, "ten cong nhan san xuat", 1),
        _anchored(
            "full_name", "Họ và tên", FieldKind.TEXT, "ten cong nhan san xuat", 2
        ),
        FieldSpec(
            key="share",
            label="SP sợi của công nhân",
            kind=FieldKind.NUMBER,
            aliases=("sp soi",),
        ),
    ),
)


def _sewing_template(
    key: str,
    label: str,
    department: str,
    keywords: tuple[str, ...],
    excludes: tuple[str, ...],
    item_field: FieldSpec,
    extra_fields: tuple[FieldSpec, ...] = (),
) -> ReportTemplate:
    """Regular and jumbo sewing share one layout: a sewer plus a cutter or stacker per row."""
    return ReportTemplate(
        key=key,
        label=label,
        department=department,
        unit="cái",
        file_keywords=keywords,
        file_excludes=excludes,
        sheet_hints=("chi tiet",),
        date_field="date",
        quantity_field="total",
        group_by=("date",),
        detail=DetailFields(
            item_fields=("item",),
            step_fields=("step",),
            machine_fields=tuple(f.key for f in extra_fields if f.key == "machine"),
            shift_fields=tuple(f.key for f in extra_fields if f.key == "shift"),
            overtime_field="overtime",
            overtime_quantity_field=next(
                (f.key for f in extra_fields if f.key == "overtime_output"), None
            ),
        ),
        worker_slots=(
            WorkerSlot(
                role="May",
                code_field="code_sew",
                quantity_field="share_sew",
                name_fields=("name_sew", "worker_sew"),
                role_field="position",
            ),
            WorkerSlot(
                role="Cắt",
                code_field="code_stack",
                quantity_field="share_cut",
                name_fields=("name_stack", "worker_stack"),
            ),
            WorkerSlot(
                role="Xếp",
                code_field="code_stack",
                quantity_field="share_stack",
                name_fields=("name_stack", "worker_stack"),
            ),
        ),
        fields=(
            FieldSpec(
                key="date",
                label="Ngày sản xuất",
                kind=FieldKind.DATE,
                aliases=("ngay sx", "ngay san xuat"),
                required=True,
            ),
            FieldSpec(
                key="total",
                label="Tổng SP",
                kind=FieldKind.NUMBER,
                aliases=("tong sp",),
                required=True,
            ),
            FieldSpec(
                key="worker_sew",
                label="Tên CN may",
                kind=FieldKind.TEXT,
                aliases=("ten cn may",),
            ),
            item_field,
            # "CÔNG ĐOẠN SX" heads two columns; this is the one right after the order number.
            _anchored("step", "Công đoạn", FieldKind.CODE, "lsx", 1),
            _overtime(),
            *extra_fields,
            _anchored("worker_stack", "Tên CN xếp", FieldKind.TEXT, "ten cn may", 1),
            _code("code_sew", "Mã số may", "ma so may"),
            _code("code_stack", "Mã số xếp", "ma xep"),
            FieldSpec(
                key="position",
                label="Chức vụ",
                kind=FieldKind.TEXT,
                aliases=("chuc vu",),
            ),
            FieldSpec(
                key="name_sew",
                label="Họ và tên may",
                kind=FieldKind.TEXT,
                aliases=("ho va ten may",),
            ),
            FieldSpec(
                key="name_stack",
                label="Họ và tên xếp",
                kind=FieldKind.TEXT,
                aliases=("ho va ten xep",),
            ),
            _anchored("share_sew", "SP may", FieldKind.NUMBER, "san pham tinh luong"),
            _anchored(
                "share_cut", "SP cắt", FieldKind.NUMBER, "san pham tinh luong", 1
            ),
            _anchored(
                "share_stack", "SP xếp", FieldKind.NUMBER, "san pham tinh luong", 2
            ),
        ),
    )


SEWING = _sewing_template(
    "may",
    "5. May",
    "May",
    ("may",),
    ("jumbo",),
    _code("item", "Mã hàng", "ma cong doan"),
    (_machine(), _shift()),
)
JUMBO_SEWING = _sewing_template(
    "jumbo",
    "6. May Jumbo",
    "May Jumbo",
    ("jumbo",),
    (),
    _code("item", "Mã thành phẩm", "ma thanh pham"),
    (
        # "TỔNG SP" is this plus the regular-hours column beside it.
        FieldSpec(
            key="overtime_output",
            label="Sản lượng tăng ca",
            kind=FieldKind.NUMBER,
            aliases=("san luong tang ca",),
        ),
    ),
)

BLOWING = ReportTemplate(
    key="o",
    label="7. Máy Ó",
    department="Máy Ó",
    unit="kg",
    file_keywords=("o",),
    sheet_hints=("chi tiet",),
    date_field="date",
    quantity_field="weight",
    group_by=("date",),
    detail=DetailFields(shift_fields=("shift",)),
    worker_slots=(
        WorkerSlot(
            role="CN01",
            code_field="code_1",
            quantity_field="share_1",
            name_fields=("worker_1",),
        ),
        WorkerSlot(
            role="CN02",
            code_field="code_2",
            quantity_field="share_2",
            name_fields=("worker_2",),
        ),
        WorkerSlot(
            role="CN03",
            code_field="code_3",
            quantity_field="share_3",
            name_fields=("worker_3",),
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
        FieldSpec(key="shift", label="Ca", kind=FieldKind.TEXT, aliases=("ca",)),
        FieldSpec(
            key="weight",
            label="Tổng KG",
            kind=FieldKind.NUMBER,
            aliases=("kg",),
            required=True,
        ),
        _anchored("worker_1", "Tên CN01", FieldKind.TEXT, "ten"),
        _anchored("worker_2", "Tên CN02", FieldKind.TEXT, "ten", 1),
        _anchored("worker_3", "Tên CN03", FieldKind.TEXT, "ten", 2),
        _anchored("code_1", "Mã số CN01", FieldKind.CODE, "ten", 3),
        _anchored("code_2", "Mã số CN02", FieldKind.CODE, "ten", 4),
        _anchored("code_3", "Mã số CN03", FieldKind.CODE, "ten", 5),
        _anchored("share_1", "SP CN01", FieldKind.NUMBER, "san pham"),
        _anchored("share_2", "SP CN02", FieldKind.NUMBER, "san pham", 1),
        _anchored("share_3", "SP CN03", FieldKind.NUMBER, "san pham", 2),
    ),
)

TEMPLATES: tuple[ReportTemplate, ...] = (
    WEAVING,
    PRINTING,
    CUTTING,
    SPINNING,
    SEWING,
    JUMBO_SEWING,
    BLOWING,
)
