"""The FastAPI application. Run: uvicorn gui_san_luong.api.app:app --port 8800

No login yet (decided by the user on 2026-11-21): keep the port inside the company network,
because anyone who reaches it can send Zalo messages.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gui_san_luong.api.context import ApiContext
from gui_san_luong.api.errors import install_error_handlers
from gui_san_luong.api.routes import (
    changes,
    meta,
    production,
    sending,
    zalo,
    zalo_links,
)
from gui_san_luong.config import api_cors_origins

API_PREFIX = "/api/v1"


def create_app(context: ApiContext | None = None) -> FastAPI:
    app = FastAPI(
        title="Gửi sản lượng API",
        version="1.0.0",
        description="Đọc file Excel sản lượng, kiểm tra, xem trước và gửi cho từng nhân "
        "viên qua Zalo OA. Mọi chức năng của trang web Streamlit. Chưa có xác thực: chỉ "
        "dùng trong mạng nội bộ.",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    app.state.context = context or ApiContext()
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (meta, production, sending, changes, zalo_links, zalo):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


app = create_app()
