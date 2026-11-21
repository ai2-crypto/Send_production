"""The two ways to send daily output: text with emoji markers, and coloured images."""

import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from gui_san_luong.services.output_dispatch import (
    RecipientPlan,
    RecipientState,
    SendOutcome,
    SendStyle,
    dispatch,
    image_uploads_this_month,
    load_day_sends,
    plan_recipients,
)
from gui_san_luong.services.output_image import (
    MAX_IMAGE_BYTES,
    ROWS_PER_IMAGE,
    fonts_available,
    image_captions,
    image_count,
    render_output_images,
)
from gui_san_luong.services.worker_output import (
    EmployeeDay,
    OutputDetail,
    TextStyle,
    WorkerOutput,
    build_messages,
    summarize_day,
)
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.zalo.client import (
    BASE_URL,
    SEND_TEXT_PATH,
    UPLOAD_IMAGE_PATH,
    ZaloClient,
)
from gui_san_luong.zalo.errors import ZaloAuthError
from gui_san_luong.zalo.schemas import MessageSource, ZaloMessage

DAY = date(2030, 1, 1)
NOW_MS = 1_900_000_000_000
NOW = datetime.fromtimestamp(NOW_MS / 1000, UTC)
LINKS = {"1": "1001", "2": "1002"}
needs_fonts = pytest.mark.skipif(not fonts_available(), reason="fonts missing")


def _employee(code: str = "1", rows: int = 2) -> EmployeeDay:
    outputs = [
        WorkerOutput(
            employee_code=code,
            employee_name="Nguyễn Văn A",
            source="det.xlsx",
            department="Dệt",
            role="Thợ chính",
            process=None,
            day=DAY,
            quantity=100.0 + row,
            unit="mét",
            base_quantity=100.0 + row,
            detail=OutputDetail(
                item="THACUSAN056100100078TRDU", machine=str(170 + row), shift="B2"
            ),
            excel_row=row + 10,
        )
        for row in range(rows)
    ]
    return summarize_day(outputs, DAY)[0]


def test_emoji_text_keeps_every_figure_of_the_plain_text() -> None:
    employee = _employee()
    plain = build_messages(employee)[0]
    emoji = build_messages(employee, TextStyle.EMOJI)[0]

    assert emoji.startswith("📅 Sản lượng ngày 01/01/2030\n👤 Nguyễn Văn A – MS 1")
    assert "1) 🕐 Ca B2 · ⚙️ Máy 170\n   📦 THACUSAN056100100078TRDU\n" in emoji
    assert "= 👉 100 (SL thực nhận)" in emoji
    assert "1) Ca B2, máy 170, THACUSAN056100100078TRDU" in plain
    assert "👉" not in plain


def test_rows_are_split_between_images_with_the_total_in_every_caption() -> None:
    employee = _employee(rows=ROWS_PER_IMAGE + 1)

    assert image_count(employee) == 2
    captions = image_captions(employee)
    assert captions[0].endswith("(ảnh 1/2)\nTỔNG: 1.155 mét")
    assert captions[1].startswith("Sản lượng ngày 01/01/2030 – Nguyễn Văn A – MS 1")


@needs_fonts
def test_images_are_readable_files_zalo_accepts() -> None:
    images = render_output_images(_employee(rows=ROWS_PER_IMAGE + 1))

    assert len(images) == 2
    for image in images:
        assert len(image.data) <= MAX_IMAGE_BYTES
        with Image.open(io.BytesIO(image.data)) as opened:
            assert opened.format in {"PNG", "JPEG"}
            assert opened.width == 1800


@pytest.fixture
def zalo() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[ZaloClient]:
    with ZaloClient("fake-token") as zalo_client:
        yield zalo_client


def _uploaded(attachment_id: str = "att-1") -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"attachment_id": attachment_id}, "error": 0}
    )


def _sent() -> httpx.Response:
    return httpx.Response(200, json={"data": {"message_id": "m"}, "error": 0})


def test_an_image_is_uploaded_then_sent_with_its_caption(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    upload = zalo.post(UPLOAD_IMAGE_PATH).mock(return_value=_uploaded("att-9"))
    send = zalo.post(SEND_TEXT_PATH).mock(return_value=_sent())

    attachment_id = client.upload_image(b"\x89PNG fake", "a.png")
    result = client.send_image("1001", attachment_id, "Tổng: 5 mét")

    assert upload.calls.last.request.headers["access_token"] == "fake-token"
    assert b'name="file"' in upload.calls.last.request.content
    body = json.loads(send.calls.last.request.content)
    assert body["message"]["text"] == "Tổng: 5 mét"
    element = body["message"]["attachment"]["payload"]["elements"][0]
    assert element == {"media_type": "image", "attachment_id": "att-9"}
    assert result.message_id == "m"


def test_a_refused_upload_raises_its_zalo_error(
    zalo: respx.MockRouter, client: ZaloClient
) -> None:
    zalo.post(UPLOAD_IMAGE_PATH).mock(
        return_value=httpx.Response(200, json={"error": -216, "message": "x"})
    )

    with pytest.raises(ZaloAuthError):
        client.upload_image(b"data", "a.png")


def _archive() -> MessageArchive:
    return MessageArchive(
        tuple(
            Conversation(
                user_id,
                None,
                (
                    ZaloMessage(
                        message_id=user_id,
                        src=MessageSource.FROM_USER,
                        time=NOW_MS - 60_000,
                        from_id=user_id,
                        to_id="oa",
                    ),
                ),
            )
            for user_id in LINKS.values()
        )
    )


def _plans(db: Path, style: SendStyle, *codes: str) -> list[RecipientPlan]:
    records = load_day_sends(db, DAY.isoformat())
    employees = [_employee(code) for code in codes]
    return plan_recipients(employees, LINKS, _archive(), records, NOW, False, style)


@needs_fonts
def test_text_and_images_are_tracked_apart(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    zalo.post(UPLOAD_IMAGE_PATH).mock(return_value=_uploaded())
    zalo.post(SEND_TEXT_PATH).mock(return_value=_sent())

    (text,) = dispatch(db, client, _plans(db, SendStyle.TEXT, "1"), "local")
    (image_plan,) = _plans(db, SendStyle.IMAGE, "1")
    assert text.outcome is SendOutcome.SENT
    assert image_plan.state is RecipientState.READY

    (image,) = dispatch(db, client, [image_plan], "local")
    assert image.outcome is SendOutcome.SENT
    assert _plans(db, SendStyle.IMAGE, "1")[0].state is RecipientState.ALREADY_SENT
    assert image_uploads_this_month(db, datetime.now(UTC)) == 1


@needs_fonts
def test_a_token_refused_at_upload_stops_the_image_batch(
    zalo: respx.MockRouter, client: ZaloClient, tmp_path: Path
) -> None:
    db = tmp_path / "test.db"
    upload = zalo.post(UPLOAD_IMAGE_PATH).mock(
        return_value=httpx.Response(200, json={"error": -216, "message": "x"})
    )
    send = zalo.post(SEND_TEXT_PATH).mock(return_value=_sent())

    results = dispatch(db, client, _plans(db, SendStyle.IMAGE, "1", "2"), "local")

    assert [r.outcome for r in results] == [SendOutcome.FAILED, SendOutcome.STOPPED]
    assert upload.call_count == 1
    assert send.call_count == 0
    # Nothing reached the person, so the image can be sent again later.
    assert _plans(db, SendStyle.IMAGE, "1")[0].state is RecipientState.READY
