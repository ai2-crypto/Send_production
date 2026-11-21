"""Step 4: send the selected employees their day's output over Zalo, after one confirmation."""

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import streamlit as st

from gui_san_luong.domain.send_window import (
    FREE_WINDOW_MS,
    SEND_WINDOW_MS,
    Eligibility,
)
from gui_san_luong.services.output_dispatch import (
    MONTHLY_IMAGE_UPLOADS,
    DispatchStorageError,
    RecipientPlan,
    RecipientResult,
    RecipientState,
    SendOutcome,
    SendStyle,
    dispatch,
    image_uploads_this_month,
    load_day_sends,
    plan_recipients,
)
from gui_san_luong.services.output_image import fonts_available
from gui_san_luong.services.worker_output import EmployeeDay
from gui_san_luong.services.zalo_texts import (
    RECIPIENT_STATE_TEXTS,
    send_result_text,
)
from gui_san_luong.ui.zalo_state import (
    TOKEN_MISSING,
    access_token,
    epoch_ms,
    format_vietnam_time,
    loaded_archive,
    rescan_button,
)
from gui_san_luong.zalo.client import ZaloClient

# There is no login yet, so a send cannot be attributed to a person.
LOCAL_ACTOR = "local"
RESULTS_KEY = "output_send_results"
WINDOW_LABELS = {
    Eligibility.FREE: "🟢 miễn phí (trong 48 giờ)",
    Eligibility.PAID: "🟡 có thể tính phí (48 giờ – 7 ngày)",
}
MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
# Ready people first, the soonest to lose their free window at the top.
STATE_ORDER = list(RECIPIENT_STATE_TEXTS)
WINDOW_ORDER = [Eligibility.FREE, Eligibility.PAID, None, Eligibility.NEEDS_INTERACTION]
OUTCOME_LABELS = {
    SendOutcome.SENT: "✅ Đã gửi",
    SendOutcome.FAILED: "❌ Lỗi",
    SendOutcome.UNKNOWN: "❓ Không rõ",
    SendOutcome.STOPPED: "⏸ Chưa gửi (lô đã dừng)",
}


def _count(number: int) -> str:
    return f"{number:,}".replace(",", ".")


def plan_day(
    employees: Sequence[EmployeeDay],
    links: Mapping[str, str],
    day: date,
    db_path: Path,
    resend_unknown: bool = False,
    style: SendStyle = SendStyle.TEXT,
) -> list[RecipientPlan] | None:
    try:
        records = load_day_sends(db_path, day.isoformat())
    except DispatchStorageError as error:
        st.error(str(error), icon=":material/error:")
        return None
    return plan_recipients(
        employees,
        links,
        loaded_archive(),
        records,
        datetime.now(UTC),
        resend_unknown,
        style,
    )


def _show_results(day: date) -> None:
    results: list[RecipientResult] | None = st.session_state.get(RESULTS_KEY, {}).get(
        day
    )
    if not results:
        return
    counts = Counter(result.outcome for result in results)
    summary = ", ".join(
        f"{OUTCOME_LABELS[outcome]}: {_count(counts[outcome])}"
        for outcome in SendOutcome
        if counts[outcome]
    )
    failed = counts[SendOutcome.FAILED] + counts[SendOutcome.STOPPED]
    shown = (
        st.error
        if failed
        else st.warning
        if counts[SendOutcome.UNKNOWN]
        else st.success
    )
    shown(f"Kết quả lần gửi vừa rồi — {summary}.", icon=":material/outbox:")
    st.dataframe(
        [
            {
                "Mã NV": result.plan.employee.employee_code,
                "Họ tên": result.plan.employee.employee_name,
                "Kết quả": OUTCOME_LABELS[result.outcome],
                "Chi tiết": send_result_text(result),
            }
            for result in results
        ],
        hide_index=True,
        width="stretch",
    )


def _confirm_key(day: date, ready: Sequence[RecipientPlan]) -> str:
    # A new selection needs a new confirmation.
    signature = "|".join(f"{p.employee.employee_code}:{p.content_hash}" for p in ready)
    return f"output_send_confirm:{day}:{hashlib.sha256(signature.encode()).hexdigest()}"


def _send(token: str, day: date, plans: Sequence[RecipientPlan], db_path: Path) -> None:
    bar = st.progress(0.0, text="Đang gửi…")

    def show_progress(done: int, total: int) -> None:
        bar.progress(
            done / total, text=f"Đang gửi {_count(done)}/{_count(total)} người…"
        )

    try:
        with ZaloClient(token) as client:
            results = dispatch(db_path, client, plans, LOCAL_ACTOR, show_progress)
    except DispatchStorageError as error:
        st.error(str(error), icon=":material/error:")
        return
    finally:
        bar.empty()
    st.session_state.setdefault(RESULTS_KEY, {})[day] = results
    st.rerun()


def _duration(ms: int) -> str:
    """Time left in the largest two units: "1 ngày 3 giờ", "5 giờ 20 phút", "12 phút"."""
    days, rest = divmod(max(ms, 0), DAY_MS)
    hours, rest = divmod(rest, HOUR_MS)
    minutes = rest // MINUTE_MS
    if days:
        return f"{days} ngày {hours} giờ"
    if hours:
        return f"{hours} giờ {minutes} phút"
    return f"{minutes} phút"


def _window_cells(plan: RecipientPlan, now_ms: int) -> tuple[str, str]:
    """What the window allows now, and until when."""
    if plan.zalo_user_id is None:
        return "", ""
    last = plan.last_interaction_ms
    if last is None:
        return "🚫 Chưa thấy tin nào người này nhắn cho OA", ""
    if plan.window is Eligibility.FREE:
        until = last + FREE_WINDOW_MS
        return f"🟢 Miễn phí · còn {_duration(until - now_ms)}", format_vietnam_time(
            until
        )
    if plan.window is Eligibility.PAID:
        until = last + SEND_WINDOW_MS
        return (
            f"🟡 Có thể tính phí · còn {_duration(until - now_ms)} gửi được",
            format_vietnam_time(until),
        )
    return f"🔴 Hết hạn từ {format_vietnam_time(last + SEND_WINDOW_MS)}", ""


def _sort_key(plan: RecipientPlan) -> tuple[int, int, int, str]:
    """By state, then free before paid before unknown, then the soonest to expire."""
    last = plan.last_interaction_ms if plan.last_interaction_ms is not None else 0
    return (
        STATE_ORDER.index(plan.state),
        WINDOW_ORDER.index(plan.window),
        last,
        plan.employee.employee_code,
    )


def _plan_table(
    plans: Sequence[RecipientPlan], image_plans: Sequence[RecipientPlan]
) -> list[dict[str, Any]]:
    now_ms = epoch_ms(datetime.now(UTC))
    images = {plan.employee.employee_code: plan for plan in image_plans}
    rows = []
    for plan in sorted(plans, key=_sort_key):
        window, until = _window_cells(plan, now_ms)
        image = images.get(plan.employee.employee_code)
        rows.append(
            {
                "Mã NV": plan.employee.employee_code,
                "Họ tên": plan.employee.employee_name,
                "Dạng chữ": RECIPIENT_STATE_TEXTS[plan.state],
                "Dạng ảnh": RECIPIENT_STATE_TEXTS[image.state] if image else "",
                "Khung gửi": window,
                "Hiệu lực đến": until,
                "Số tin": _count(len(plan.pending_parts or plan.messages)),
                "Số ảnh": _count(len(image.pending_parts or image.messages))
                if image
                else "",
            }
        )
    return rows


def render_output_send(
    selected: Sequence[EmployeeDay],
    links: Mapping[str, str],
    day: date,
    db_path: Path,
) -> None:
    _show_results(day)
    if loaded_archive() is None:
        st.warning(
            "Chưa quét tin nhắn Zalo nên chưa biết ai đã tương tác với OA. Chỉ người đã "
            "nhắn cho OA mới được gửi, nên lúc này chưa gửi được cho ai.",
            icon=":material/forum:",
        )
    rescan_button(f"output_rescan:{day}")
    unknown_key = f"output_resend_unknown:{day}"
    resend_unknown = bool(st.session_state.get(unknown_key))
    plans = {
        style: plan_day(selected, links, day, db_path, resend_unknown, style)
        for style in SendStyle
    }
    text_plans, image_plans = plans[SendStyle.TEXT], plans[SendStyle.IMAGE]
    if text_plans is None or image_plans is None:
        return
    unclear = {
        plan.employee.employee_code
        for style_plans in (text_plans, image_plans)
        for plan in style_plans
        if plan.state is RecipientState.UNKNOWN_BEFORE
    }
    if unclear or resend_unknown:
        st.checkbox(
            f"Gửi lại cho {_count(len(unclear))} người mà lần trước không rõ đã gửi chưa "
            "(họ có thể nhận trùng tin)",
            key=unknown_key,
        )
    _show_summary(text_plans, image_plans)

    token = access_token()
    if not token:
        st.info(TOKEN_MISSING, icon=":material/key:")
    text_ready = _ready(text_plans)
    image_ready = _ready(image_plans)
    images = sum(len(plan.pending_parts) for plan in image_ready)
    image_problem = _image_problem(db_path, images)
    confirmed = st.checkbox(
        "Tôi đã xem trước nội dung và xác nhận gửi. Tin đã gửi không thu hồi được.",
        key=_confirm_key(day, [*text_ready, *image_ready]),
        disabled=not (token and (text_ready or image_ready)),
    )
    text_col, image_col = st.columns(2)
    with text_col:
        if st.button(
            f"Gửi dạng chữ cho {_count(len(text_ready))} nhân viên",
            type="primary",
            icon=":material/sms:",
            disabled=not (token and text_ready and confirmed),
            width="stretch",
            key=f"output_send_text:{day}",
            help="Chữ có emoji đánh dấu ca, máy, mã hàng và sản lượng.",
        ):
            _send(token, day, text_plans, db_path)
    with image_col:
        if st.button(
            f"Gửi dạng ảnh màu cho {_count(len(image_ready))} nhân viên",
            type="primary",
            icon=":material/image:",
            disabled=not (token and image_ready and confirmed and not image_problem),
            width="stretch",
            key=f"output_send_image:{day}",
            help=image_problem
            or "Mỗi 10 dòng một ảnh, kèm dòng chữ ghi tổng sản lượng.",
        ):
            _send(token, day, image_plans, db_path)


def _ready(plans: Sequence[RecipientPlan]) -> list[RecipientPlan]:
    return [plan for plan in plans if plan.state is RecipientState.READY]


def _image_problem(db_path: Path, images: int) -> str | None:
    """Why images cannot go now; also shows how much of the monthly quota is used."""
    if not fonts_available():
        st.caption(
            ":material/error: Máy này thiếu phông chữ (Segoe UI trên Windows, DejaVu trên Linux) nên chưa vẽ "
            "được ảnh."
        )
        return "Máy này thiếu phông chữ để vẽ ảnh."
    try:
        used = image_uploads_this_month(db_path, datetime.now(UTC))
    except DispatchStorageError as error:
        st.caption(str(error))
        return str(error)
    left = MONTHLY_IMAGE_UPLOADS - used
    st.caption(
        f":material/image: Gửi ảnh lần này dùng {_count(images)} lượt tải ảnh; tháng này "
        f"đã dùng {_count(used)}/{_count(MONTHLY_IMAGE_UPLOADS)} lượt (Zalo giới hạn mỗi "
        "tháng). Ảnh chỉ được Zalo lưu 7 ngày, sau đó người nhận chỉ còn thấy dòng chữ "
        "ghi tổng."
    )
    if images > left:
        return f"Không đủ lượt tải ảnh tháng này: cần {_count(images)}, còn {_count(left)}."
    return None


def _show_summary(
    text_plans: Sequence[RecipientPlan], image_plans: Sequence[RecipientPlan]
) -> None:
    """Who will get what, and who is skipped and why."""
    states = Counter(plan.state for plan in text_plans)
    text_ready, image_ready = _ready(text_plans), _ready(image_plans)
    windows = Counter(plan.window for plan in text_ready)
    texts = sum(len(plan.pending_parts) for plan in text_ready)
    images = sum(len(plan.pending_parts) for plan in image_ready)
    lines = [
        (
            f"**Dạng chữ: {_count(texts)} tin cho {_count(len(text_ready))} người · "
            f"Dạng ảnh: {_count(images)} ảnh cho {_count(len(image_ready))} người**"
        )
    ]
    lines += [
        # Icon first, so the colours line up with the table below.
        f"- {label.split(' ', 1)[0]} {_count(windows[window])} người "
        f"{label.split(' ', 1)[1]}"
        for window, label in WINDOW_LABELS.items()
        if windows[window]
    ]
    lines += [
        f"- Bỏ qua {_count(states[state])} người — {label}"
        for state, label in RECIPIENT_STATE_TEXTS.items()
        if state not in (RecipientState.READY, RecipientState.ALREADY_SENT)
        and states[state]
    ]
    sent = {
        style: sum(plan.state is RecipientState.ALREADY_SENT for plan in plans)
        for style, plans in (
            (SendStyle.TEXT, text_plans),
            (SendStyle.IMAGE, image_plans),
        )
    }
    if sent[SendStyle.TEXT] or sent[SendStyle.IMAGE]:
        lines.append(
            f"- ✅ Đã gửi đúng nội dung này: {_count(sent[SendStyle.TEXT])} người dạng "
            f"chữ, {_count(sent[SendStyle.IMAGE])} người dạng ảnh"
        )
    st.markdown("\n".join(lines))
    if windows[Eligibility.PAID]:
        st.warning(
            f"{_count(windows[Eligibility.PAID])} người đã quá 48 giờ không tương tác: "
            "Zalo có thể tính phí các tin này.",
            icon=":material/payments:",
        )
    with st.expander("Xem từng người sẽ gửi hay bỏ qua"):
        st.caption(
            "🟢 miễn phí trong 48 giờ sau lần nhắn cuối của người đó · 🟡 sau đó đến 7 ngày "
            "vẫn gửi được nhưng có thể tính phí · 🔴 quá 7 ngày Zalo từ chối · 🚫 chưa thấy "
            "tin nào người đó nhắn cho OA nên không gửi."
        )
        st.dataframe(
            _plan_table(text_plans, image_plans),
            hide_index=True,
            width="stretch",
            column_config={
                "Khung gửi": st.column_config.TextColumn("Khung gửi", width="large"),
            },
        )
