"""Phần chung: kiểm tra máy chủ và danh sách loại báo cáo."""

from fastapi import APIRouter

from gui_san_luong.api.schemas import Template, TemplateField
from gui_san_luong.domain.production_templates import TEMPLATES

router = APIRouter(tags=["Chung"])


@router.get("/health", summary="Máy chủ đang chạy")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/templates", summary="Các loại báo cáo và trường dữ liệu của từng loại")
def templates() -> list[Template]:
    return [
        Template(
            key=template.key,
            label=template.label,
            department=template.department,
            unit=template.unit,
            fields=[
                TemplateField(
                    key=spec.key,
                    label=spec.label,
                    kind=spec.kind.value,
                    required=spec.required,
                )
                for spec in template.fields
            ],
        )
        for template in TEMPLATES
    ]
