"""Sản lượng vừa sửa: số liệu khác lần tải file trước, chờ gửi lại tin."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from gui_san_luong.api.context import ApiContext, get_context
from gui_san_luong.api.schemas import ChangeOut, HandledIn
from gui_san_luong.api.views import change_out
from gui_san_luong.services.output_changes import (
    load_open_changes,
    mark_changes_handled,
)

router = APIRouter(prefix="/output-changes", tags=["Sản lượng vừa sửa"])
Context = Annotated[ApiContext, Depends(get_context)]


@router.get("", summary="Các dòng sản lượng đã đổi và chưa đánh dấu đã xử lý")
def open_changes(context: Context) -> list[ChangeOut]:
    return [change_out(change) for change in load_open_changes(context.db_path)]


@router.post(
    "/handled",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Đánh dấu đã gửi lại tin cho các dòng này",
)
def mark_handled(context: Context, body: HandledIn) -> Response:
    mark_changes_handled(context.db_path, body.ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
