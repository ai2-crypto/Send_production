"""Settings read from the environment, or from a .env file next to pyproject.toml.

A real environment variable wins over the .env file. Values are never logged or shown.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).parents[2]
DATA_DIR = PROJECT_DIR / "data"
DOTENV_PATH = PROJECT_DIR / ".env"
DATABASE_PATH_VARIABLE = "GUI_SAN_LUONG_DB"
DEFAULT_DATABASE_PATH = DATA_DIR / "gui_san_luong.db"
IMPORT_CACHE_PATH_VARIABLE = "GUI_SAN_LUONG_IMPORT_CACHE"
# Kept apart from the main database so it can be deleted at any time without losing anything.
DEFAULT_IMPORT_CACHE_PATH = DATA_DIR / "import_cache.db"
ZALO_ACCESS_TOKEN_VARIABLE = "ZALO_OA_ACCESS_TOKEN"
ZALO_APP_ID_VARIABLE = "ZALO_APP_ID"
ZALO_SECRET_KEY_VARIABLE = "ZALO_SECRET_KEY"
TOKEN_ENCRYPTION_KEY_VARIABLE = "TOKEN_ENCRYPTION_KEY"


def read_dotenv(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines, # comments and an `export ` prefix are allowed.

    Quotes around a value are removed. A missing or unreadable file gives no values.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def setting(name: str, dotenv_path: Path = DOTENV_PATH) -> str:
    """The value of a setting, read again on every call so an edited .env applies at once."""
    configured = os.environ.get(name, "").strip()
    return configured or read_dotenv(dotenv_path).get(name, "").strip()


def _path(variable: str, default: Path) -> Path:
    configured = setting(variable)
    return Path(configured) if configured else default


def database_path() -> Path:
    return _path(DATABASE_PATH_VARIABLE, DEFAULT_DATABASE_PATH)


def import_cache_path() -> Path:
    return _path(IMPORT_CACHE_PATH_VARIABLE, DEFAULT_IMPORT_CACHE_PATH)


def zalo_access_token() -> str:
    """The OA access token set in the environment or .env; empty when there is none."""
    return setting(ZALO_ACCESS_TOKEN_VARIABLE)


def zalo_app_id() -> str:
    return setting(ZALO_APP_ID_VARIABLE)


def zalo_secret_key() -> str:
    """The app's secret key, sent only to Zalo's token endpoint."""
    return setting(ZALO_SECRET_KEY_VARIABLE)


def token_encryption_key() -> str:
    """The Fernet key that encrypts the stored OA tokens."""
    return setting(TOKEN_ENCRYPTION_KEY_VARIABLE)


API_CORS_ORIGINS_VARIABLE = "API_CORS_ORIGINS"


def api_cors_origins() -> list[str]:
    """Frontend origins allowed to call the API, comma-separated; every origin when unset."""
    configured = setting(API_CORS_ORIGINS_VARIABLE)
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or ["*"]
