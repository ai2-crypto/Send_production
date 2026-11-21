"""What the routes need from outside: paths, the Zalo client and the clock.

Kept on app.state so tests can swap any of them for a fake.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request

from gui_san_luong.api.imports import ImportStore
from gui_san_luong.config import database_path, import_cache_path
from gui_san_luong.services.zalo_token import resolve_access_token
from gui_san_luong.zalo.client import ZaloClient

# Who the audit log names for actions taken through the API; it has no login yet.
API_ACTOR = "api"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ApiContext:
    db_path: Path = field(default_factory=database_path)
    cache_path: Path = field(default_factory=import_cache_path)
    imports: ImportStore = field(default_factory=ImportStore)
    # The access token to call Zalo with; empty when none is set up.
    access_token: Callable[[Path], str] = resolve_access_token
    zalo_client: Callable[[str], ZaloClient] = ZaloClient
    now: Callable[[], datetime] = _utc_now


def get_context(request: Request) -> ApiContext:
    context: ApiContext = request.app.state.context
    return context
