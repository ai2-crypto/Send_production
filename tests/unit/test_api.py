"""The HTTP API against a fake Zalo and a temporary database: nothing real is sent or read."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from openpyxl import Workbook

from gui_san_luong.api import routes
from gui_san_luong.api.app import create_app
from gui_san_luong.api.context import ApiContext
from gui_san_luong.api.imports import ImportStore
from gui_san_luong.services.message_store import store_archive
from gui_san_luong.services.zalo_messages import Conversation, MessageArchive
from gui_san_luong.services.zalo_token import TokenSettings
from gui_san_luong.zalo.client import (
    BASE_URL,
    OAUTH_TOKEN_URL,
    SEND_TEXT_PATH,
    ZaloClient,
)
from gui_san_luong.zalo.schemas import MessageSource, ZaloMessage

DAY = date(2030, 1, 1)
NOW = datetime(2030, 1, 2, 8, 0, tzinfo=UTC)
USER = "5890956125949515459"
API = "/api/v1"
FILE_NAME = "7.BANG LUONG O T01-2030.xlsx"
SETTINGS = TokenSettings("1234567890", "fake-secret", Fernet.generate_key().decode())


def _blowing_workbook() -> bytes:
    """A made-up blowing report: two workers, codes 10 and 20, on DAY."""
    rows: list[list[Any]] = [
        [None, None, 90] + [None] * 6 + [60, 30, 0],
        ["NGÀY", "CA", "KG", "TÊN", None, None, "MÃ SỐ", "MÃ SỐ", "MÃ SỐ"]
        + ["SẢN PHẨM", None, None],
        [None] * 3 + [None] * 6 + ["CN01", "CN02", "CN02"],
        [datetime(2030, 1, 1), "N", 90, "an", "bo", None, 10, 20, None, 60, 30, 0],
    ]
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "CHI TIET"
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


WORKBOOK = _blowing_workbook()
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def zalo() -> Iterator[respx.MockRouter]:
    # Every request must match a route here, so no real Zalo call can slip through.
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def client(
    tmp_path: Path, zalo: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(
        routes.zalo.TokenSettings, "from_environment", classmethod(lambda cls: SETTINGS)
    )
    context = ApiContext(
        db_path=tmp_path / "app.db",
        cache_path=tmp_path / "cache.db",
        imports=ImportStore(now=lambda: NOW),
        access_token=lambda _path: "fake-token",
        zalo_client=lambda token: ZaloClient(
            token, http=httpx.Client(base_url=BASE_URL), sleep=lambda _: None
        ),
        now=lambda: NOW,
    )
    with TestClient(create_app(context)) as test_client:
        yield test_client


def _upload(client: TestClient, *files: tuple[str, bytes]) -> dict[str, Any]:
    response = client.post(
        f"{API}/imports",
        files=[("files", (name, data, XLSX)) for name, data in files],
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _sent(message_id: str = "m1") -> httpx.Response:
    return httpx.Response(
        200, json={"error": 0, "message": "Success", "data": {"message_id": message_id}}
    )


def _user_texted(db: Path, user_id: str = USER) -> None:
    message = ZaloMessage(
        message_id="u1",
        src=MessageSource.FROM_USER,
        time=int((NOW - timedelta(hours=1)).timestamp() * 1000),
        message="xin chào",
        from_id=user_id,
        to_id="1",
    )
    store_archive(db, MessageArchive((Conversation(user_id, "An", (message,)),), NOW))


# ---------------------------------------------------------------- general


def test_health_and_report_types(client: TestClient) -> None:
    assert client.get(f"{API}/health").json() == {"status": "ok"}
    keys = [template["key"] for template in client.get(f"{API}/templates").json()]
    assert "o" in keys


def test_errors_have_one_shape(client: TestClient) -> None:
    response = client.get(f"{API}/imports/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_openapi_spec_is_served(client: TestClient) -> None:
    spec = client.get(f"{API}/openapi.json").json()
    assert f"{API}/imports/{{import_id}}/days/{{day}}/sends" in spec["paths"]


# ---------------------------------------------------------------- reading files


def test_upload_reads_the_file_and_skips_a_duplicate(client: TestClient) -> None:
    body = _upload(client, (FILE_NAME, WORKBOOK), ("copy.xlsx", WORKBOOK))
    assert [file["status"] for file in body["files"]] == ["ready"]
    assert body["files"][0]["template_key"] == "o"
    assert body["files"][0]["rows_read"] == 1
    assert body["skipped"][0]["name"] == "copy.xlsx"
    assert body["days"] == [DAY.isoformat()]


def test_only_excel_files_are_accepted(client: TestClient) -> None:
    response = client.post(
        f"{API}/imports", files=[("files", ("notes.txt", b"hi", "text/plain"))]
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_file_detail_shows_the_columns_and_changing_settings_rereads(
    client: TestClient,
) -> None:
    body = _upload(client, (FILE_NAME, WORKBOOK))
    import_id, file_id = body["import_id"], body["files"][0]["file_id"]
    detail = client.get(f"{API}/imports/{import_id}/files/{file_id}").json()
    assert detail["start_row"] == 4
    assert detail["sheets"] == [{"name": "CHI TIET", "hidden": False}]
    assert all(field["status"] == "auto" for field in detail["fields"])
    assert detail["preview_rows"][0]["Dòng Excel"] == 4

    response = client.put(
        f"{API}/imports/{import_id}/files/{file_id}/settings",
        json={"template_key": "khong-co"},
    )
    assert response.status_code == 422

    response = client.put(
        f"{API}/imports/{import_id}/files/{file_id}/settings",
        json={"mapping": {}},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "excluded"
    assert client.get(f"{API}/imports/{import_id}").json()["days"] == []


def test_day_lists_each_employee_with_messages_and_issues(client: TestClient) -> None:
    import_id = _upload(client, (FILE_NAME, WORKBOOK))["import_id"]
    day = client.get(f"{API}/imports/{import_id}/days/{DAY}").json()
    assert [e["employee_code"] for e in day["employees"]] == ["10", "20"]
    first = day["employees"][0]
    assert first["lines"][0]["quantity"] == 60.0
    assert first["zalo"] == {
        "linked": False,
        "zalo_user_id": None,
        "text_state": "not_linked",
        "image_state": "not_linked",
        "sent": False,
    }
    messages = client.get(
        f"{API}/imports/{import_id}/days/{DAY}/employees/10/messages"
    ).json()["messages"]
    assert messages and "60" in messages[0]

    issues = client.get(f"{API}/imports/{import_id}/issues", params={"day": DAY}).json()
    # The made-up file gives nicknames only, so the full names are flagged.
    assert any("họ tên" in issue["title"] for issue in issues["issues"])

    departments = client.get(f"{API}/imports/{import_id}/days/{DAY}/departments").json()
    assert departments[0]["employee_codes"] == ["10", "20"]


# ---------------------------------------------------------------- links


def test_links_can_be_set_listed_and_removed(client: TestClient) -> None:
    import_id = _upload(client, (FILE_NAME, WORKBOOK))["import_id"]
    response = client.put(f"{API}/zalo-links/10", json={"zalo_user_id": USER})
    assert response.json() == {"saved": 1, "removed": 0}
    links = client.get(f"{API}/zalo-links", params={"import_id": import_id}).json()
    assert links["linked"] == 1 and links["not_linked"] == 1
    assert client.get(f"{API}/zalo-links/linked").json()[0]["zalo_user_id"] == USER

    response = client.put(f"{API}/zalo-links/20", json={"zalo_user_id": "abc"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_links"

    assert client.delete(f"{API}/zalo-links/10").status_code == 204
    assert client.get(f"{API}/zalo-links/linked").json() == []


# ---------------------------------------------------------------- sending


def _plan(client: TestClient, import_id: str, codes: list[str]) -> dict[str, Any]:
    response = client.post(
        f"{API}/imports/{import_id}/days/{DAY}/send-plan",
        json={"style": "text", "employee_codes": codes},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_nobody_is_sent_to_without_a_link_and_a_message_to_the_oa(
    client: TestClient,
) -> None:
    import_id = _upload(client, (FILE_NAME, WORKBOOK))["import_id"]
    client.put(f"{API}/zalo-links/10", json={"zalo_user_id": USER})
    plan = _plan(client, import_id, ["10", "20"])
    states = {r["employee_code"]: r["state"] for r in plan["recipients"]}
    assert states == {"10": "no_interaction", "20": "not_linked"}
    response = client.post(
        f"{API}/imports/{import_id}/days/{DAY}/sends",
        json={
            "style": "text",
            "employee_codes": ["10", "20"],
            "confirmation": plan["confirmation"],
        },
    )
    assert response.status_code == 409


def test_a_confirmed_send_goes_once_and_a_stale_one_is_refused(
    client: TestClient, zalo: respx.MockRouter, tmp_path: Path
) -> None:
    import_id = _upload(client, (FILE_NAME, WORKBOOK))["import_id"]
    client.put(f"{API}/zalo-links/10", json={"zalo_user_id": USER})
    _user_texted(tmp_path / "app.db")
    route = zalo.post(f"{BASE_URL}{SEND_TEXT_PATH}").mock(return_value=_sent())

    plan = _plan(client, import_id, ["10"])
    assert plan["ready_count"] == 1 and plan["recipients"][0]["window"] == "free"

    stale = client.post(
        f"{API}/imports/{import_id}/days/{DAY}/sends",
        json={"style": "text", "employee_codes": ["10"], "confirmation": "old"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "confirmation_stale"
    assert route.call_count == 0

    sent = client.post(
        f"{API}/imports/{import_id}/days/{DAY}/sends",
        json={
            "style": "text",
            "employee_codes": ["10"],
            "confirmation": plan["confirmation"],
        },
    ).json()
    assert sent["sent"] == 1 and sent["results"][0]["outcome"] == "sent"
    assert route.call_count == plan["parts_to_send"]

    again = _plan(client, import_id, ["10"])
    assert again["recipients"][0]["state"] == "already_sent"
    assert again["ready_count"] == 0
    day = client.get(f"{API}/imports/{import_id}/days/{DAY}").json()
    assert day["sent_count"] == 1
    records = client.get(f"{API}/sends/{DAY}").json()
    assert {record["status"] for record in records} == {"sent"}


# ---------------------------------------------------------------- Zalo tab


def test_test_message_needs_confirmation(
    client: TestClient, zalo: respx.MockRouter
) -> None:
    route = zalo.post(f"{BASE_URL}{SEND_TEXT_PATH}").mock(return_value=_sent())
    body = {"user_id": USER, "text": "thử", "confirm": False}
    assert client.post(f"{API}/zalo/test-message", json=body).status_code == 409
    assert route.call_count == 0
    body["confirm"] = True
    result = client.post(f"{API}/zalo/test-message", json=body).json()
    assert result["status"] == "sent" and route.call_count == 1


def test_free_window_and_stored_messages(client: TestClient, tmp_path: Path) -> None:
    assert client.get(f"{API}/zalo/messages").json() is None
    _user_texted(tmp_path / "app.db")
    archive = client.get(f"{API}/zalo/messages").json()
    assert archive["from_user_count"] == 1
    users = client.get(f"{API}/zalo/free-window").json()["users"]
    assert users[0]["user_id"] == USER
    assert client.delete(f"{API}/zalo/messages").json()["deleted"] >= 1


def test_token_is_saved_refreshed_and_never_returned(
    client: TestClient, zalo: respx.MockRouter
) -> None:
    zalo.post(OAUTH_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "A2", "refresh_token": "R2", "expires_in": "90000"},
        )
    )
    assert client.get(f"{API}/zalo/token").json()["stored"] is False
    body = client.put(
        f"{API}/zalo/token", json={"access_token": "A1", "refresh_token": "R1"}
    ).json()
    assert body["refreshed"] is True
    assert body["status"]["access_expiry_known"] is True
    assert "A2" not in str(body) and "R2" not in str(body)
    assert client.delete(f"{API}/zalo/token").status_code == 204
    assert client.get(f"{API}/zalo/token").json()["stored"] is False
