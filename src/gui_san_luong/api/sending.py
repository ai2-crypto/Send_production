"""Who would receive what for a day, and the confirmation that guards a send.

A send cannot be recalled, so a frontend first asks for the plan, shows it, and sends back
the plan's confirmation. The confirmation hashes exactly who is ready and what they would
get, so a changed selection or edited figures make the send refuse instead of going out.
"""

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date

from gui_san_luong.api.context import ApiContext
from gui_san_luong.domain.send_window import Eligibility
from gui_san_luong.services.message_store import load_stored_archive
from gui_san_luong.services.output_dispatch import (
    MONTHLY_IMAGE_UPLOADS,
    RecipientPlan,
    RecipientState,
    SendStyle,
    image_uploads_this_month,
    load_day_sends,
    plan_recipients,
)
from gui_san_luong.services.output_image import fonts_available
from gui_san_luong.services.production_review import count_text
from gui_san_luong.services.worker_output import EmployeeDay


def day_plans(
    context: ApiContext,
    employees: Sequence[EmployeeDay],
    links: Mapping[str, str],
    day: date,
    style: SendStyle,
    resend_unknown: bool = False,
) -> list[RecipientPlan]:
    """Raises DispatchStorageError or MessageStorageError when the database fails."""
    return plan_recipients(
        employees,
        links,
        load_stored_archive(context.db_path),
        load_day_sends(context.db_path, day.isoformat()),
        context.now(),
        resend_unknown,
        style,
    )


def ready(plans: Sequence[RecipientPlan]) -> list[RecipientPlan]:
    return [plan for plan in plans if plan.state is RecipientState.READY]


def confirmation(day: date, style: SendStyle, plans: Sequence[RecipientPlan]) -> str:
    signature = "|".join(
        f"{plan.employee.employee_code}:{plan.zalo_user_id}:{plan.content_hash}:"
        f"{','.join(str(part) for part in plan.pending_parts)}"
        for plan in ready(plans)
    )
    text = f"{day.isoformat()}|{style}|{signature}"
    return hashlib.sha256(text.encode()).hexdigest()


def paid_count(plans: Sequence[RecipientPlan]) -> int:
    return sum(plan.window is Eligibility.PAID for plan in ready(plans))


def image_problem(context: ApiContext, needed: int) -> tuple[int, str | None]:
    """Uploads used this month, and why images cannot go now (None when they can)."""
    if not fonts_available():
        return 0, "Máy chủ thiếu phông chữ để vẽ ảnh."
    used = image_uploads_this_month(context.db_path, context.now())
    left = MONTHLY_IMAGE_UPLOADS - used
    if needed > left:
        return used, (
            f"Không đủ lượt tải ảnh tháng này: cần {count_text(needed)}, còn "
            f"{count_text(left)}."
        )
    return used, None
