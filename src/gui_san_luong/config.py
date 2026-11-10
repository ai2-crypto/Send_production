"""Settings read from the environment."""

import os
from pathlib import Path

DATABASE_PATH_VARIABLE = "GUI_SAN_LUONG_DB"
DEFAULT_DATABASE_PATH = Path(__file__).parents[2] / "data" / "gui_san_luong.db"


def database_path() -> Path:
    configured = os.environ.get(DATABASE_PATH_VARIABLE, "").strip()
    return Path(configured) if configured else DEFAULT_DATABASE_PATH
