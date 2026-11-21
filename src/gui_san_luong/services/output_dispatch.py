"""Send each selected employee their day's output over Zalo, at most once per content.

A message cannot be recalled, so:
- every part is recorded as 'sending' before Zalo is called and updated after;
- a part already sent with the same content to the same account is never sent again;
- a part whose outcome is unknown is not resent unless the operator asks for it;
- a token, permission, quota or night-time refusal stops the whole batch, since every later
  message would fail the same way.

This sends from the page while the operator waits. The planned worker queue will take over,
so a closed browser cannot cut a batch short.
"""

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from gui_san_luong.db.connection import open_database
from gui_san_luong.db.output_sends import (
    SendRecord,
    count_style_since,
    fetch_day_sends,
    finish_send,
    mark_interrupted,
    start_send,
)
from gui_san_luong.domain.send_window import Eligibility
from gui_san_luong.services.output_image import (
    OutputImage,
    OutputImageError,
    image_captions,
    render_output_images,
)
from gui_san_luong.services.worker_output import EmployeeDay, TextStyle, build_messages
from gui_san_luong.services.zalo_messages import MessageArchive
from gui_san_luong.services.zalo_send import eligibility, last_interaction_ms
from gui_san_luong.zalo.client import ZaloClient
from gui_san_luong.zalo.errors import (
    AUTH_CODES,
    CONFIGURATION_CODES,
    ZaloError,
    ZaloErrorCode,
)
from gui_san_luong.zalo.schemas import SendResult, SendStatus

STORAGE_ERROR = "Không đọc hoặc ghi được nhật ký gửi tin. Hãy thử lại."
NIGHT_CODE = -234
OA_QUOTA_CODE = -211
# Every later message would fail the same way, so the batch stops at the first one.
BATCH_STOP_CODES = frozenset(
    {
        *AUTH_CODES,
        *CONFIGURATION_CODES,
        ZaloErrorCode.RATE_LIMITED,
        NIGHT_CODE,
        OA_QUOTA_CODE,
    }
)
STATUS_SENDING = "sending"
# Zalo lets an OA upload this many images a month.
MONTHLY_IMAGE_UPLOADS = 5000
UNCLEAR_STATUSES = frozenset({SendStatus.UNKNOWN.value, STATUS_SENDING})


class DispatchStorageError(Exception):
    """A user-facing reason why the send log could not be read or written."""


class SendStyle(StrEnum):
    # Text with an emoji before each kind of figure.
    TEXT = "text"
    # A coloured image per ten rows, each with the day's total as its caption.
    IMAGE = "image"


class RecipientState(StrEnum):
    READY = "ready"
    NOT_LINKED = "not_linked"
    # No message from this person to the OA was found, or messages were not loaded.
    NO_INTERACTION = "no_interaction"
    NEEDS_INTERACTION = "needs_interaction"
    ALREADY_SENT = "already_sent"
    UNKNOWN_BEFORE = "unknown_before"


@dataclass(frozen=True)
class RecipientPlan:
    employee: EmployeeDay
    zalo_user_id: str | None
    # The texts to send, or for images the caption of each image.
    messages: tuple[str, ...]
    content_hash: str
    state: RecipientState
    # None when no message from the user is known, so there is no window.
    window: Eligibility | None = None
    # One-based numbers of the parts still to send.
    pending_parts: tuple[int, ...] = ()
    # The user's last message to the OA, in epoch milliseconds, when it is known.
    last_interaction_ms: int | None = None
    style: SendStyle = SendStyle.TEXT


class SendOutcome(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
    # Not attempted: an earlier refusal stopped the batch.
    STOPPED = "stopped"


@dataclass(frozen=True)
class RecipientResult:
    plan: RecipientPlan
    outcome: SendOutcome
    error_code: int | None = None
    error_message: str | None = None
    message_ids: tuple[str, ...] = field(default=())


def content_hash(messages: Sequence[str]) -> str:
    return hashlib.sha256("\n\0".join(messages).encode()).hexdigest()


def _parts(employee: EmployeeDay, style: SendStyle) -> tuple[tuple[str, ...], str]:
    """What each part carries, and a fingerprint of the figures in that style.

    The fingerprint includes the style, so a person sent the text can still be sent the
    images; the images are drawn from the same figures as the plain text, so that text
    fingerprints them without drawing anything.
    """
    if style is SendStyle.IMAGE:
        captions = tuple(image_captions(employee))
        figures = build_messages(employee)
        return captions, content_hash([style.value, *figures])
    messages = tuple(build_messages(employee, TextStyle.EMOJI))
    return messages, content_hash([style.value, *messages])


def image_uploads_this_month(db_path: Path, now: datetime) -> int:
    """Images this app uploaded this month; uploads from elsewhere are not counted."""
    since = now.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    try:
        with open_database(db_path) as connection:
            return count_style_since(
                connection, SendStyle.IMAGE.value, since.isoformat()
            )
    except sqlite3.Error as error:
        raise DispatchStorageError(STORAGE_ERROR) from error


def load_day_sends(db_path: Path, day: str) -> list[SendRecord]:
    try:
        with open_database(db_path) as connection:
            return fetch_day_sends(connection, day)
    except sqlite3.Error as error:
        raise DispatchStorageError(STORAGE_ERROR) from error


def _part_statuses(
    records: Iterable[SendRecord], code: str, zalo_user_id: str, digest: str
) -> dict[int, str]:
    """The latest status of each part of this exact content to this exact account."""
    statuses: dict[int, str] = {}
    for record in records:
        if (
            record.employee_code == code
            and record.zalo_user_id == zalo_user_id
            and record.content_hash == digest
        ):
            statuses[record.part] = record.status
    return statuses


def plan_recipients(
    employees: Iterable[EmployeeDay],
    links: Mapping[str, str],
    archive: MessageArchive | None,
    records: Sequence[SendRecord],
    now: datetime,
    resend_unknown: bool = False,
    style: SendStyle = SendStyle.TEXT,
) -> list[RecipientPlan]:
    conversations = (
        {c.user_id: c for c in archive.conversations} if archive is not None else {}
    )
    plans = []
    for employee in employees:
        messages, digest = _parts(employee, style)
        zalo_user_id = links.get(employee.employee_code)
        if zalo_user_id is None:
            plans.append(
                RecipientPlan(
                    employee,
                    None,
                    messages,
                    digest,
                    RecipientState.NOT_LINKED,
                    style=style,
                )
            )
            continue
        statuses = _part_statuses(records, employee.employee_code, zalo_user_id, digest)
        parts = range(1, len(messages) + 1)
        pending = tuple(
            part
            for part in parts
            if statuses.get(part) != SendStatus.SENT
            and (resend_unknown or statuses.get(part) not in UNCLEAR_STATUSES)
        )
        conversation = conversations.get(zalo_user_id)
        last = last_interaction_ms(conversation.messages) if conversation else None
        window = (
            eligibility(conversation.messages, now)
            if conversation is not None and last is not None
            else None
        )
        if all(statuses.get(part) == SendStatus.SENT for part in parts):
            state = RecipientState.ALREADY_SENT
        elif not pending:
            state = RecipientState.UNKNOWN_BEFORE
        elif last is None:
            # Only someone known to have written to the OA is sent to: Zalo refuses or
            # charges the others, and an unknown account may not be the employee at all.
            state = RecipientState.NO_INTERACTION
        elif window is Eligibility.NEEDS_INTERACTION:
            state = RecipientState.NEEDS_INTERACTION
        else:
            state = RecipientState.READY
        plans.append(
            RecipientPlan(
                employee,
                zalo_user_id,
                messages,
                digest,
                state,
                window,
                pending,
                last,
                style,
            )
        )
    return plans


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _send_one(
    connection: sqlite3.Connection,
    client: ZaloClient,
    plan: RecipientPlan,
    actor: str,
) -> RecipientResult:
    assert plan.zalo_user_id is not None
    images: list[OutputImage] = []
    if plan.style is SendStyle.IMAGE:
        try:
            images = render_output_images(plan.employee)
        except OutputImageError as error:
            # Nothing was sent: the image could not even be drawn.
            return RecipientResult(plan, SendOutcome.FAILED, error_message=str(error))
    message_ids: list[str] = []
    for part in plan.pending_parts:
        send_id = start_send(
            connection,
            plan.employee.day.isoformat(),
            SendRecord(
                plan.employee.employee_code,
                plan.zalo_user_id,
                plan.content_hash,
                part,
                len(plan.messages),
                STATUS_SENDING,
                None,
                "",
                plan.style.value,
            ),
            actor,
            _now(),
        )
        if plan.style is SendStyle.IMAGE:
            result = _send_image(client, plan.zalo_user_id, images[part - 1])
        else:
            result = client.send_text(plan.zalo_user_id, plan.messages[part - 1])
        finish_send(
            connection,
            send_id,
            result.status.value,
            result.message_id,
            result.error_code,
            _now(),
        )
        if result.status is not SendStatus.SENT:
            return RecipientResult(
                plan,
                SendOutcome(result.status.value),
                result.error_code,
                result.error_message,
                tuple(message_ids),
            )
        message_ids.append(result.message_id or "")
    return RecipientResult(plan, SendOutcome.SENT, message_ids=tuple(message_ids))


def _send_image(client: ZaloClient, user_id: str, image: OutputImage) -> SendResult:
    """Upload, then send. A failed upload sent nothing, so it is a plain failure."""
    try:
        attachment_id = client.upload_image(image.data, image.file_name)
    except ZaloError as error:
        return SendResult(
            SendStatus.FAILED, error_code=error.code, error_message=error.message
        )
    return client.send_image(user_id, attachment_id, image.caption)


def dispatch(
    db_path: Path,
    client: ZaloClient,
    plans: Iterable[RecipientPlan],
    actor: str,
    progress: Callable[[int, int], None] | None = None,
) -> list[RecipientResult]:
    """Send every ready plan in turn. Plans in any other state are left alone."""
    ready = [plan for plan in plans if plan.state is RecipientState.READY]
    results: list[RecipientResult] = []
    stop_code: int | None = None
    try:
        with open_database(db_path) as connection:
            mark_interrupted(connection, _now())
            for index, plan in enumerate(ready, start=1):
                if progress:
                    progress(index, len(ready))
                if stop_code is not None:
                    results.append(
                        RecipientResult(plan, SendOutcome.STOPPED, stop_code)
                    )
                    continue
                result = _send_one(connection, client, plan, actor)
                results.append(result)
                if result.error_code in BATCH_STOP_CODES:
                    stop_code = result.error_code
    except sqlite3.Error as error:
        raise DispatchStorageError(STORAGE_ERROR) from error
    return results
