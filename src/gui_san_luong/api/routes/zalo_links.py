"""Tab “Nối Zalo”: mã nhân viên ↔ Zalo user id, và mã nhân viên lấy từ tin nhắn."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from gui_san_luong.api.context import API_ACTOR, ApiContext, get_context
from gui_san_luong.api.errors import ApiError, ErrorCode, not_found
from gui_san_luong.api.schemas import (
    ApplyProposalsIn,
    LinkChangesOut,
    LinkOut,
    LinkRowOut,
    LinksIn,
    LinksOut,
    LinkUpdate,
    ProposalOut,
    ProposalsOut,
)
from gui_san_luong.api.views import from_epoch_ms
from gui_san_luong.db.zalo_links import LinkRecord
from gui_san_luong.services.message_store import load_stored_archive
from gui_san_luong.services.worker_output import EmployeeIdentity, list_employees
from gui_san_luong.services.zalo_codes import (
    ProposalStatus,
    apply_proposals,
    latest_claims,
    plan_proposals,
)
from gui_san_luong.services.zalo_links import (
    LinkChanges,
    LinkRow,
    build_editable_rows,
    load_links,
    save_links,
)
from gui_san_luong.services.zalo_texts import proposal_status_text

router = APIRouter(prefix="/zalo-links", tags=["Nối Zalo"])
Context = Annotated[ApiContext, Depends(get_context)]
ImportId = Annotated[
    str | None,
    Query(
        description="Lượt tải file để thêm nhân viên trong file vào danh sách (không bắt "
        "buộc)."
    ),
]


def _roster(context: ApiContext, import_id: str | None) -> list[EmployeeIdentity]:
    if not import_id:
        return []
    session = context.imports.get(import_id)
    if session is None:
        raise not_found("Không có lượt tải file này, hoặc đã hết hạn.")
    with session.lock:
        return list_employees(session.combined().outputs)


def _changes_out(changes: LinkChanges) -> LinkChangesOut:
    return LinkChangesOut(saved=len(changes.upserts), removed=len(changes.removals))


def _rows(links: list[LinkRecord]) -> list[LinkRow]:
    return [
        LinkRow(link.employee_code, link.employee_name, link.zalo_user_id)
        for link in links
    ]


@router.get("", summary="Danh sách nối, gồm cả nhân viên trong file chưa nối")
def get_links(context: Context, import_id: ImportId = None) -> LinksOut:
    stored = load_links(context.db_path)
    rows = build_editable_rows(stored, _roster(context, import_id))
    return LinksOut(
        linked=len(stored),
        not_linked=sum(not row.zalo_user_id for row in rows),
        rows=[
            LinkRowOut(
                employee_code=row.employee_code,
                employee_name=row.employee_name,
                zalo_user_id=row.zalo_user_id,
                departments=row.departments,
            )
            for row in rows
        ],
    )


@router.get("/linked", summary="Chỉ những người đã nối")
def get_linked(context: Context) -> list[LinkOut]:
    return [
        LinkOut(
            employee_code=link.employee_code,
            employee_name=link.employee_name,
            zalo_user_id=link.zalo_user_id,
        )
        for link in load_links(context.db_path)
    ]


@router.put("", summary="Lưu cả bảng nối (như nút “Lưu thay đổi”)")
def replace_links(context: Context, body: LinksIn) -> LinkChangesOut:
    rows = [
        LinkRow(row.employee_code, row.employee_name, row.zalo_user_id)
        for row in body.rows
    ]
    return _changes_out(save_links(context.db_path, rows, API_ACTOR))


@router.put("/{employee_code}", summary="Nối hoặc đổi Zalo của một nhân viên")
def set_link(context: Context, employee_code: str, body: LinkUpdate) -> LinkChangesOut:
    links = {link.employee_code: link for link in load_links(context.db_path)}
    current = links.get(employee_code)
    name = body.employee_name or (current.employee_name if current else employee_code)
    links[employee_code] = LinkRecord(employee_code, name, body.zalo_user_id)
    return _changes_out(
        save_links(context.db_path, _rows(list(links.values())), API_ACTOR)
    )


@router.delete(
    "/{employee_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Gỡ nối Zalo của một nhân viên",
)
def remove_link(context: Context, employee_code: str) -> Response:
    links = load_links(context.db_path)
    if not any(link.employee_code == employee_code for link in links):
        raise not_found(f"Mã {employee_code} chưa nối Zalo.")
    kept = [link for link in links if link.employee_code != employee_code]
    save_links(context.db_path, _rows(kept), API_ACTOR)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/proposals",
    summary="Mã nhân viên mọi người nhắn cho OA, đề xuất để nối",
    description="Dựa trên tin nhắn đã quét (POST /zalo/messages/scan). Ai cũng có thể "
    "nhắn mã của người khác: hãy so tên Zalo với họ tên trước khi áp dụng.",
)
def get_proposals(context: Context, import_id: ImportId = None) -> ProposalsOut:
    archive = load_stored_archive(context.db_path)
    if archive is None:
        return ProposalsOut(
            messages_loaded_at=None, texted_codes=0, already_linked=0, proposals=[]
        )
    proposals = plan_proposals(
        latest_claims(archive),
        load_links(context.db_path),
        _roster(context, import_id),
    )
    pending = [p for p in proposals if p.status is not ProposalStatus.UNCHANGED]
    return ProposalsOut(
        messages_loaded_at=archive.fetched_at,
        texted_codes=len(proposals),
        already_linked=len(proposals) - len(pending),
        proposals=[
            ProposalOut(
                employee_code=proposal.employee_code,
                employee_name=proposal.employee_name,
                zalo_user_id=proposal.claim.user_id,
                zalo_display_name=proposal.claim.display_name,
                message=proposal.claim.message,
                sent_at=from_epoch_ms(proposal.claim.sent_ms),
                status=proposal.status,
                status_text=proposal_status_text(proposal),
                replaced=proposal.replaced,
                selected_by_default=proposal.selected_by_default,
            )
            for proposal in pending
        ],
    )


@router.post(
    "/proposals/apply", summary="Áp dụng các đề xuất đã chọn theo mã nhân viên"
)
def apply(
    context: Context, body: ApplyProposalsIn, import_id: ImportId = None
) -> LinkChangesOut:
    archive = load_stored_archive(context.db_path)
    if archive is None:
        raise ApiError(
            409,
            ErrorCode.INVALID_REQUEST,
            "Chưa quét tin nhắn nên chưa có đề xuất nào.",
        )
    stored = load_links(context.db_path)
    wanted = set(body.employee_codes)
    chosen = [
        proposal
        for proposal in plan_proposals(
            latest_claims(archive), stored, _roster(context, import_id)
        )
        if proposal.employee_code in wanted
        and proposal.status is not ProposalStatus.UNCHANGED
    ]
    if not chosen:
        raise not_found("Không có đề xuất nào cho các mã này.")
    return _changes_out(apply_proposals(context.db_path, stored, chosen, API_ACTOR))
