"""Bước 1–4 của trang: tải file, chỉnh cách đọc, kiểm tra, nhân viên và xem trước tin."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from gui_san_luong.api.context import ApiContext, get_context
from gui_san_luong.api.errors import ApiError, ErrorCode, not_found
from gui_san_luong.api.imports import (
    MAX_FILES,
    ImportSession,
    StoredFile,
    compare_with_history,
    disk_reader,
    read_file,
)
from gui_san_luong.api.schemas import (
    DayOut,
    DepartmentPreview,
    EmployeeOut,
    FileDetail,
    ImageOut,
    ImagesOut,
    ImportOut,
    IssuesOut,
    MessagesOut,
    ReadSettingsIn,
    ZaloStatus,
)
from gui_san_luong.api.sending import day_plans
from gui_san_luong.api.views import (
    change_out,
    changes_by_employee,
    file_detail,
    import_out,
    issues_out,
    line_out,
)
from gui_san_luong.services.excel_import import MAX_FILE_BYTES
from gui_san_luong.services.output_changes import load_open_changes
from gui_san_luong.services.output_dispatch import RecipientState, SendStyle
from gui_san_luong.services.output_image import OutputImageError, render_output_images
from gui_san_luong.services.production_review import (
    ReadSettings,
    general_issues,
    issues_for_day,
    template_by_key,
)
from gui_san_luong.services.worker_output import (
    EmployeeDay,
    TextStyle,
    build_messages,
    group_by_department,
)
from gui_san_luong.services.zalo_links import load_links

router = APIRouter(prefix="/imports", tags=["Sản lượng"])
Context = Annotated[ApiContext, Depends(get_context)]
EXCEL_SUFFIXES = (".xlsx", ".xlsm")


def _session(context: ApiContext, import_id: str) -> ImportSession:
    session = context.imports.get(import_id)
    if session is None:
        raise not_found(
            "Không có lượt tải file này, hoặc đã hết hạn. Hãy tải file lên lại."
        )
    return session


def _stored(session: ImportSession, file_id: str) -> StoredFile:
    stored = session.file(file_id)
    if stored is None or stored.reading is None:
        raise not_found("Không có file này trong lượt tải.")
    return stored


def _employee(session: ImportSession, day: date, code: str) -> EmployeeDay:
    for employee in session.employees(day):
        if employee.employee_code == code:
            return employee
    raise not_found(f"Không có sản lượng của mã {code} ngày {day:%d/%m/%Y}.")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Tải các file Excel sản lượng lên",
)
def create_import(
    context: Context,
    files: Annotated[list[UploadFile], File(description="Các file .xlsx/.xlsm")],
) -> ImportOut:
    if not files or len(files) > MAX_FILES:
        raise ApiError(
            422, ErrorCode.INVALID_REQUEST, f"Hãy gửi từ 1 đến {MAX_FILES} file."
        )
    uploads = []
    for upload in files:
        name = upload.filename or "file.xlsx"
        if not name.lower().endswith(EXCEL_SUFFIXES):
            raise ApiError(
                422,
                ErrorCode.INVALID_REQUEST,
                f"{name}: chỉ nhận file .xlsx hoặc .xlsm.",
            )
        # One byte over the limit is enough to know the file is too big.
        uploads.append((name, upload.file.read(MAX_FILE_BYTES + 1)))
    session = context.imports.create(uploads, disk_reader(context.cache_path))
    with session.lock:
        compare_with_history(session, context.db_path)
        return import_out(session)


@router.get("/{import_id}", summary="Tình trạng một lượt tải file")
def get_import(context: Context, import_id: str) -> ImportOut:
    session = _session(context, import_id)
    with session.lock:
        return import_out(session)


@router.delete(
    "/{import_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Bỏ lượt tải file (xóa khỏi bộ nhớ máy chủ)",
)
def delete_import(context: Context, import_id: str) -> Response:
    if not context.imports.delete(import_id):
        raise not_found("Không có lượt tải file này.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{import_id}/files/{file_id}",
    summary="Cách đọc và kết quả đọc một file",
)
def get_file(context: Context, import_id: str, file_id: str) -> FileDetail:
    session = _session(context, import_id)
    with session.lock:
        return file_detail(_stored(session, file_id))


@router.put(
    "/{import_id}/files/{file_id}/settings",
    summary="Đổi loại báo cáo, sheet, dòng bắt đầu hoặc cột của một file",
)
def update_file_settings(
    context: Context, import_id: str, file_id: str, settings: ReadSettingsIn
) -> FileDetail:
    session = _session(context, import_id)
    if settings.template_key and template_by_key(settings.template_key) is None:
        raise ApiError(
            422,
            ErrorCode.INVALID_REQUEST,
            "Không có loại báo cáo này (xem /templates).",
        )
    with session.lock:
        stored = _stored(session, file_id)
        stored.settings = ReadSettings(
            settings.template_key,
            settings.sheet,
            settings.start_row,
            settings.mapping,
            tuple(settings.extra_columns),
        )
        read_file(stored, disk_reader(context.cache_path))
        compare_with_history(session, context.db_path)
        return file_detail(stored)


@router.get(
    "/{import_id}/issues",
    summary="Những điểm cần kiểm tra trước khi gửi",
)
def get_issues(
    context: Context,
    import_id: str,
    day: Annotated[
        date | None,
        Query(description="Bỏ trống để chỉ lấy các điểm không phụ thuộc ngày."),
    ] = None,
) -> IssuesOut:
    session = _session(context, import_id)
    with session.lock:
        outcomes, rosters = session.outcomes(), session.rosters()
        combined = session.combined()
        issues = (
            issues_for_day(outcomes, rosters, combined, day)
            if day
            else general_issues(outcomes, rosters, combined)
        )
        return issues_out(issues, day)


@router.get(
    "/{import_id}/days/{day}",
    summary="Nhân viên có sản lượng trong ngày, kèm tình trạng Zalo và số liệu vừa sửa",
)
def get_day(context: Context, import_id: str, day: date) -> DayOut:
    session = _session(context, import_id)
    with session.lock:
        employees = session.employees(day)
    links = {
        link.employee_code: link.zalo_user_id for link in load_links(context.db_path)
    }
    plans = {
        style: {
            plan.employee.employee_code: plan
            for plan in day_plans(context, employees, links, day, style)
        }
        for style in SendStyle
    }
    changes = changes_by_employee(load_open_changes(context.db_path), day)
    rows = []
    for employee in employees:
        code = employee.employee_code
        text_plan = plans[SendStyle.TEXT].get(code)
        image_plan = plans[SendStyle.IMAGE].get(code)
        sent = any(
            plan is not None and plan.state is RecipientState.ALREADY_SENT
            for plan in (text_plan, image_plan)
        )
        rows.append(
            EmployeeOut(
                employee_code=code,
                employee_name=employee.employee_name,
                lines=[line_out(line) for line in employee.lines],
                departments_in_period=list(employee.period_departments),
                transferred=employee.was_transferred,
                zalo=ZaloStatus(
                    linked=code in links,
                    zalo_user_id=links.get(code),
                    text_state=text_plan.state if text_plan else None,
                    image_state=image_plan.state if image_plan else None,
                    sent=sent,
                ),
                changes=[change_out(change) for change in changes.get(code, [])],
            )
        )
    return DayOut(
        day=day,
        employee_count=len(rows),
        transferred_count=sum(row.transferred for row in rows),
        sent_count=sum(row.zalo.sent for row in rows),
        employees=rows,
    )


@router.get(
    "/{import_id}/days/{day}/departments",
    summary="Nhân viên trong ngày theo bộ phận (để xem trước theo bộ phận)",
)
def get_departments(
    context: Context, import_id: str, day: date
) -> list[DepartmentPreview]:
    session = _session(context, import_id)
    with session.lock:
        groups = group_by_department(session.employees(day))
    return [
        DepartmentPreview(
            department=name,
            employee_codes=[employee.employee_code for employee in members],
        )
        for name, members in groups.items()
    ]


@router.get(
    "/{import_id}/days/{day}/employees/{employee_code}/messages",
    summary="Đúng các tin dạng chữ sẽ gửi cho một nhân viên",
)
def get_messages(
    context: Context, import_id: str, day: date, employee_code: str
) -> MessagesOut:
    session = _session(context, import_id)
    with session.lock:
        employee = _employee(session, day, employee_code)
    return MessagesOut(
        employee_code=employee.employee_code,
        employee_name=employee.employee_name,
        messages=build_messages(employee, TextStyle.EMOJI),
    )


@router.get(
    "/{import_id}/days/{day}/employees/{employee_code}/images",
    summary="Danh sách ảnh màu sẽ gửi cho một nhân viên",
)
def get_images(
    context: Context, import_id: str, day: date, employee_code: str
) -> ImagesOut:
    session = _session(context, import_id)
    with session.lock:
        employee = _employee(session, day, employee_code)
    try:
        images = render_output_images(employee)
    except OutputImageError as error:
        raise ApiError(503, ErrorCode.IMAGE_NOT_AVAILABLE, str(error)) from error
    base = (
        f"/api/v1/imports/{import_id}/days/{day.isoformat()}/employees/{employee_code}"
    )
    return ImagesOut(
        employee_code=employee_code,
        images=[
            ImageOut(index=index, caption=image.caption, url=f"{base}/images/{index}")
            for index, image in enumerate(images, start=1)
        ],
    )


@router.get(
    "/{import_id}/days/{day}/employees/{employee_code}/images/{index}",
    summary="Tải một ảnh màu (PNG/JPEG)",
    response_class=Response,
    responses={200: {"content": {"image/png": {}, "image/jpeg": {}}}},
)
def get_image(
    context: Context, import_id: str, day: date, employee_code: str, index: int
) -> Response:
    session = _session(context, import_id)
    with session.lock:
        employee = _employee(session, day, employee_code)
    try:
        images = render_output_images(employee)
    except OutputImageError as error:
        raise ApiError(503, ErrorCode.IMAGE_NOT_AVAILABLE, str(error)) from error
    if not 1 <= index <= len(images):
        raise not_found("Không có ảnh này.")
    image = images[index - 1]
    media_type = "image/png" if image.file_name.endswith(".png") else "image/jpeg"
    return Response(content=image.data, media_type=media_type)
