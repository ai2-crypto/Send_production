"""Request and response bodies. Field names are English; messages for people are Vietnamese."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gui_san_luong.services.output_dispatch import (
    RecipientState,
    SendOutcome,
    SendStyle,
)
from gui_san_luong.services.production_review import FileStatus, Severity
from gui_san_luong.services.zalo_codes import ProposalStatus
from gui_san_luong.zalo.schemas import MessageSource


class Model(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------- report types


class TemplateField(Model):
    key: str
    label: str
    kind: str
    required: bool


class Template(Model):
    key: str
    label: str
    department: str
    unit: str | None
    fields: list[TemplateField]


# ---------------------------------------------------------------- imports


class ColumnOptionOut(Model):
    column: int = Field(description="Chỉ số cột, bắt đầu từ 0 (A = 0).")
    letter: str
    header: str
    sample: str


class FieldMatchOut(Model):
    field_key: str
    label: str
    required: bool
    status: Literal["auto", "ambiguous", "missing"]
    detected_column: int | None
    candidates: list[int]
    chosen_column: int | None


class SheetOut(Model):
    name: str
    hidden: bool


class TotalOut(Model):
    label: str
    counted: float
    all_rows: float
    stated: float | None
    matches_stated: bool | None


class RejectedRowOut(Model):
    row: int
    reason: str


class FileSummary(Model):
    file_id: str
    name: str
    status: FileStatus
    status_text: str
    template_key: str | None
    template_label: str
    sheet: str
    rows_read: int
    rejected_rows: int
    warnings: list[str]
    excluded_because: str | None


class FileDetail(FileSummary):
    template_suggested: bool
    sheets: list[SheetOut]
    sheet_found: bool
    start_row: int | None
    start_row_detected: bool
    fields: list[FieldMatchOut]
    columns: list[ColumnOptionOut]
    extra_columns: list[int]
    totals: list[TotalOut]
    rejected: list[RejectedRowOut]
    blank_rows: int
    total_rows: int
    repeated_rows: int
    groups: list[dict[str, Any]] = Field(
        description="Tổng theo ngày (và theo nhóm của loại báo cáo) như trong file."
    )
    preview_rows: list[dict[str, Any]] = Field(
        description="Tối đa 200 dòng đầu đã đọc, khóa là tên cột hiển thị."
    )


class SkippedFileOut(Model):
    name: str
    reason: str


class ImportOut(Model):
    import_id: str
    created_at: datetime
    expires_after_minutes: int
    files: list[FileSummary]
    skipped: list[SkippedFileOut]
    days: list[date]
    changed_rows: int = Field(
        description="Số dòng sản lượng khác lần tải trước, phát hiện ở lần đọc gần nhất."
    )
    edited_files: list[str]


class ReadSettingsIn(Model):
    template_key: str | None = Field(
        None, description="Bỏ trống để tự nhận theo tên file."
    )
    sheet: str | None = Field(None, description="Bỏ trống để tự tìm sheet có đủ cột.")
    start_row: int | None = Field(
        None, ge=2, description="Dòng bắt đầu dữ liệu (đánh số như Excel)."
    )
    mapping: dict[str, int] | None = Field(
        None,
        description="field_key → chỉ số cột (A = 0). Bỏ trống để dùng cột tự nhận; "
        "trường không có trong mapping sẽ không được lấy.",
    )
    extra_columns: list[int] = Field(
        default_factory=list, description="Cột lấy thêm chỉ để xem."
    )


# ---------------------------------------------------------------- review


class IssueOut(Model):
    severity: Severity
    title: str
    detail: str
    rows: list[dict[str, Any]]


class IssuesOut(Model):
    day: date | None
    errors: int
    warnings: int
    issues: list[IssueOut]


class OutputLineOut(Model):
    department: str
    role: str
    process: str | None
    quantity: float
    unit: str | None
    text: str = Field(
        description="Dòng tóm tắt như cột “Sản lượng trong ngày” trên web."
    )


class ChangeOut(Model):
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
    difference: float
    excel_row: int | None


class ZaloStatus(Model):
    linked: bool
    zalo_user_id: str | None
    text_state: RecipientState | None
    image_state: RecipientState | None
    sent: bool = Field(
        description="Đã nhận đúng nội dung hiện tại (dạng chữ hoặc ảnh)."
    )


class EmployeeOut(Model):
    employee_code: str
    employee_name: str
    lines: list[OutputLineOut]
    departments_in_period: list[str]
    transferred: bool
    zalo: ZaloStatus
    changes: list[ChangeOut]


class DayOut(Model):
    day: date
    employee_count: int
    transferred_count: int
    sent_count: int
    employees: list[EmployeeOut]


class MessagesOut(Model):
    employee_code: str
    employee_name: str
    messages: list[str] = Field(
        description="Đúng các tin dạng chữ sẽ gửi (có emoji), theo thứ tự."
    )


class ImageOut(Model):
    index: int
    caption: str
    url: str = Field(description="Đường dẫn tải ảnh PNG (tương đối với gốc API).")


class ImagesOut(Model):
    employee_code: str
    images: list[ImageOut]


class DepartmentPreview(Model):
    department: str
    employee_codes: list[str]


# ---------------------------------------------------------------- sending


class SendSelection(Model):
    style: SendStyle = SendStyle.TEXT
    employee_codes: list[str] = Field(
        description="Mã nhân viên được chọn gửi. Người không có trong ngày bị bỏ qua."
    )
    resend_unknown: bool = Field(
        False,
        description="Gửi lại cả người mà lần trước không rõ đã gửi chưa (có thể nhận trùng).",
    )


class RecipientPlanOut(Model):
    employee_code: str
    employee_name: str
    zalo_user_id: str | None
    state: RecipientState
    state_text: str
    window: Literal["free", "paid", "needs_interaction"] | None
    last_interaction_at: datetime | None
    parts: int
    pending_parts: list[int]


class ImageQuota(Model):
    used_this_month: int
    monthly_limit: int
    needed: int
    problem: str | None


class SendPlanOut(Model):
    day: date
    style: SendStyle
    recipients: list[RecipientPlanOut]
    ready_count: int
    parts_to_send: int
    paid_count: int = Field(description="Người quá 48 giờ: Zalo có thể tính phí.")
    unknown_before_count: int
    token_ready: bool
    image_quota: ImageQuota | None
    confirmation: str = Field(
        description="Gửi lại giá trị này ở POST .../sends để xác nhận đúng danh sách và "
        "nội dung vừa xem. Đổi người chọn hay nội dung thì phải lấy kế hoạch mới."
    )


class SendRequest(SendSelection):
    confirmation: str = Field(description="Giá trị `confirmation` của send-plan.")


class SendResultOut(Model):
    employee_code: str
    employee_name: str
    outcome: SendOutcome
    detail: str
    error_code: int | None
    message_ids: list[str]


class SendResultsOut(Model):
    day: date
    style: SendStyle
    sent: int
    failed: int
    unknown: int
    stopped: int
    results: list[SendResultOut]


class SendRecordOut(Model):
    employee_code: str
    zalo_user_id: str
    style: str
    part: int
    parts: int
    status: str
    zalo_message_id: str | None
    updated_at: str


# ---------------------------------------------------------------- changes


class HandledIn(Model):
    ids: list[int] = Field(min_length=1)


# ---------------------------------------------------------------- Zalo links


class LinkOut(Model):
    employee_code: str
    employee_name: str
    zalo_user_id: str


class LinkRowOut(Model):
    employee_code: str
    employee_name: str
    zalo_user_id: str
    departments: str


class LinksOut(Model):
    linked: int
    not_linked: int
    rows: list[LinkRowOut]


class LinkIn(Model):
    employee_code: str
    employee_name: str = ""
    zalo_user_id: str = Field(description="Chuỗi chữ số; để trống là gỡ nối.")


class LinksIn(Model):
    rows: list[LinkIn] = Field(
        description="Cả bảng: mã nào đang nối mà không có trong bảng, hoặc để trống "
        "zalo_user_id, sẽ bị gỡ."
    )


class LinkUpdate(Model):
    zalo_user_id: str
    employee_name: str | None = None


class LinkChangesOut(Model):
    saved: int
    removed: int


class ProposalOut(Model):
    employee_code: str
    employee_name: str | None
    zalo_user_id: str
    zalo_display_name: str | None
    message: str
    sent_at: datetime
    status: ProposalStatus
    status_text: str
    replaced: str | None
    selected_by_default: bool


class ProposalsOut(Model):
    messages_loaded_at: datetime | None
    texted_codes: int
    already_linked: int
    proposals: list[ProposalOut]


class ApplyProposalsIn(Model):
    employee_codes: list[str] = Field(min_length=1)


# ---------------------------------------------------------------- Zalo messages


class MessageOut(Model):
    message_id: str
    direction: Literal["from_user", "from_oa"]
    sent_at: datetime
    type: str | None
    text: str | None
    attachment: str | None


class ConversationOut(Model):
    user_id: str
    display_name: str | None
    truncated: bool
    messages: list[MessageOut]


class ArchiveOut(Model):
    fetched_at: datetime
    user_count: int
    message_count: int
    from_user_count: int
    conversations: list[ConversationOut]


class ScanIn(Model):
    extra_user_ids: list[str] = Field(
        default_factory=list,
        description="User id Zalo cần lấy thêm (người nhắn đã lâu có thể không có trong "
        "danh sách gần đây).",
    )
    only_extra: bool = Field(False, description="Chỉ tải các user id đã nêu.")


class ForgetOut(Model):
    deleted: int


class ActiveUserOut(Model):
    user_id: str
    display_name: str | None
    employee_code: str | None
    last_interaction_at: datetime
    free_until: datetime


class FreeWindowOut(Model):
    messages_loaded_at: datetime | None
    users: list[ActiveUserOut]


class TestMessageIn(Model):
    user_id: str = Field(pattern=r"^\d+$")
    text: str = Field(min_length=1, max_length=2000)
    confirm: bool = Field(description="Phải là true: tin đã gửi không thu hồi được.")


class TestMessageOut(Model):
    status: str
    message_id: str | None
    error_code: int | None
    detail: str
    audit_failed: bool


# ---------------------------------------------------------------- token


class TokenStatusOut(Model):
    stored: bool
    missing_settings: list[str]
    access_expires_at: datetime | None
    access_expiry_known: bool
    refresh_expires_at: datetime | None
    updated_at: datetime | None
    last_error: str | None
    needs_reauthorization: bool
    usable: bool = Field(
        description="Có access token để gọi Zalo (đã lưu hoặc trong .env)."
    )


class TokenIn(Model):
    access_token: str = Field(min_length=1)
    refresh_token: str = Field(min_length=1)


class RefreshOut(Model):
    refreshed: bool
    status: TokenStatusOut


def message_direction(source: MessageSource) -> Literal["from_user", "from_oa"]:
    return "from_user" if source is MessageSource.FROM_USER else "from_oa"
