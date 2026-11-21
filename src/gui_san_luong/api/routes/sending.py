"""Bước 4: kế hoạch gửi, gửi sản lượng qua Zalo và kết quả gửi."""

from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends

from gui_san_luong.api.context import API_ACTOR, ApiContext, get_context
from gui_san_luong.api.errors import ApiError, ErrorCode, not_found
from gui_san_luong.api.schemas import (
    ImageQuota,
    SendPlanOut,
    SendRecordOut,
    SendRequest,
    SendResultOut,
    SendResultsOut,
    SendSelection,
)
from gui_san_luong.api.sending import (
    confirmation,
    day_plans,
    image_problem,
    paid_count,
    ready,
)
from gui_san_luong.api.views import plan_out
from gui_san_luong.services.output_dispatch import (
    MONTHLY_IMAGE_UPLOADS,
    RecipientPlan,
    RecipientState,
    SendOutcome,
    SendStyle,
    dispatch,
    load_day_sends,
)
from gui_san_luong.services.zalo_links import load_links
from gui_san_luong.services.zalo_texts import send_result_text

router = APIRouter(tags=["Gửi sản lượng"])
Context = Annotated[ApiContext, Depends(get_context)]


def _selected_plans(
    context: ApiContext, import_id: str, day: date, selection: SendSelection
) -> list[RecipientPlan]:
    session = context.imports.get(import_id)
    if session is None:
        raise not_found("Không có lượt tải file này, hoặc đã hết hạn. Hãy tải lại.")
    wanted = set(selection.employee_codes)
    with session.lock:
        employees = [
            employee
            for employee in session.employees(day)
            if employee.employee_code in wanted
        ]
    links = {
        link.employee_code: link.zalo_user_id for link in load_links(context.db_path)
    }
    return day_plans(
        context, employees, links, day, selection.style, selection.resend_unknown
    )


@router.post(
    "/imports/{import_id}/days/{day}/send-plan",
    summary="Xem trước ai sẽ được gửi, ai bị bỏ qua và lý do",
)
def send_plan(
    context: Context, import_id: str, day: date, selection: SendSelection
) -> SendPlanOut:
    plans = _selected_plans(context, import_id, day, selection)
    ready_plans = ready(plans)
    parts = sum(len(plan.pending_parts) for plan in ready_plans)
    quota = None
    if selection.style is SendStyle.IMAGE:
        used, problem = image_problem(context, parts)
        quota = ImageQuota(
            used_this_month=used,
            monthly_limit=MONTHLY_IMAGE_UPLOADS,
            needed=parts,
            problem=problem,
        )
    return SendPlanOut(
        day=day,
        style=selection.style,
        recipients=[plan_out(plan) for plan in plans],
        ready_count=len(ready_plans),
        parts_to_send=parts,
        paid_count=paid_count(plans),
        unknown_before_count=sum(
            plan.state is RecipientState.UNKNOWN_BEFORE for plan in plans
        ),
        token_ready=bool(context.access_token(context.db_path)),
        image_quota=quota,
        confirmation=confirmation(day, selection.style, plans),
    )


@router.post(
    "/imports/{import_id}/days/{day}/sends",
    summary="Gửi sản lượng qua Zalo (không thu hồi được)",
    description="Chỉ gửi cho người ở trạng thái `ready` trong kế hoạch. Phải gửi kèm "
    "`confirmation` lấy từ send-plan; danh sách hay nội dung đổi thì trả 409.",
)
def send(
    context: Context, import_id: str, day: date, request: SendRequest
) -> SendResultsOut:
    plans = _selected_plans(context, import_id, day, request)
    if request.confirmation != confirmation(day, request.style, plans):
        raise ApiError(
            409,
            ErrorCode.CONFIRMATION_STALE,
            "Danh sách người nhận hoặc nội dung đã đổi so với lúc xem. Hãy lấy kế hoạch "
            "gửi mới, xem lại rồi xác nhận.",
        )
    ready_plans = ready(plans)
    if not ready_plans:
        raise ApiError(
            409, ErrorCode.INVALID_REQUEST, "Không có ai đủ điều kiện để gửi."
        )
    if request.style is SendStyle.IMAGE:
        _, problem = image_problem(
            context, sum(len(plan.pending_parts) for plan in ready_plans)
        )
        if problem:
            raise ApiError(409, ErrorCode.IMAGE_NOT_AVAILABLE, problem)
    token = context.access_token(context.db_path)
    if not token:
        raise ApiError(
            409,
            ErrorCode.TOKEN_MISSING,
            "Chưa có access token Zalo: hãy lưu token (PUT /zalo/token).",
        )
    with context.zalo_client(token) as client:
        results = dispatch(context.db_path, client, ready_plans, API_ACTOR)
    counts = Counter(result.outcome for result in results)
    return SendResultsOut(
        day=day,
        style=request.style,
        sent=counts[SendOutcome.SENT],
        failed=counts[SendOutcome.FAILED],
        unknown=counts[SendOutcome.UNKNOWN],
        stopped=counts[SendOutcome.STOPPED],
        results=[
            SendResultOut(
                employee_code=result.plan.employee.employee_code,
                employee_name=result.plan.employee.employee_name,
                outcome=result.outcome,
                detail=send_result_text(result),
                error_code=result.error_code,
                message_ids=list(result.message_ids),
            )
            for result in results
        ],
    )


@router.get(
    "/sends/{day}",
    summary="Các phần tin đã gửi (hoặc không rõ) trong một ngày sản xuất",
)
def sends_of_day(context: Context, day: date) -> list[SendRecordOut]:
    return [
        SendRecordOut(
            employee_code=record.employee_code,
            zalo_user_id=record.zalo_user_id,
            style=record.style,
            part=record.part,
            parts=record.parts,
            status=record.status,
            zalo_message_id=record.zalo_message_id,
            updated_at=record.updated_at,
        )
        for record in load_day_sends(context.db_path, day.isoformat())
    ]
