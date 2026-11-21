"""Keep what was read from a workbook, so the same file is never read twice.

Reading a large report takes seconds, and operators upload the same files again after restarting
the app or after editing just one of them. Entries are keyed by the file's content and by the
source of the code that reads it, so an edited file or an updated parser always reads afresh
instead of reusing figures that may no longer be right.

The cache is a convenience: when it cannot be read or written, the file is simply read again.
"""

import hashlib
import logging
import pickle
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gui_san_luong.db.import_cache import fetch_entry, open_cache, store_entry
from gui_san_luong.domain import production_templates
from gui_san_luong.services import employee_roster, excel_import

KEEP_UNUSED_FOR = timedelta(days=30)
MAX_ENTRIES = 300
# Pickle only ever loads entries this app wrote into its own local file. Failures that mean the
# entry is unusable, such as a class that has since changed, fall back to reading the file.
UNUSABLE_ENTRY_ERRORS = (
    pickle.UnpicklingError,
    AttributeError,
    EOFError,
    ImportError,
    IndexError,
    TypeError,
    ValueError,
)

logger = logging.getLogger(__name__)


def _code_fingerprint() -> str:
    digest = hashlib.sha256()
    for module in (excel_import, employee_roster, production_templates):
        if module.__file__ is not None:
            digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()


CODE_FINGERPRINT = _code_fingerprint()


def file_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cache_key(kind: str, digest: str, params: object = ()) -> str:
    """Params must have a stable repr: tuples of strings and numbers, sorted where unordered."""
    text = f"{CODE_FINGERPRINT}|{kind}|{digest}|{params!r}"
    return hashlib.sha256(text.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _load(path: Path, key: str) -> bytes | None:
    try:
        with open_cache(path) as connection:
            return fetch_entry(connection, key, _now().isoformat())
    except sqlite3.Error:
        logger.warning("Import cache could not be read", exc_info=True)
        return None


def _save(path: Path, key: str, value: object) -> None:
    now = _now()
    try:
        blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        with open_cache(path) as connection:
            store_entry(
                connection,
                key,
                blob,
                now.isoformat(),
                (now - KEEP_UNUSED_FOR).isoformat(),
                MAX_ENTRIES,
            )
    except sqlite3.Error, pickle.PicklingError:
        logger.warning("Import cache could not be written", exc_info=True)


def cached[T](path: Path, key: str, compute: Callable[[], T]) -> T:
    """The stored value for key, or compute() saved for next time. Errors are not stored."""
    blob = _load(path, key)
    if blob is not None:
        try:
            value: T = pickle.loads(blob)
        except UNUSABLE_ENTRY_ERRORS:
            logger.warning("Ignoring an unusable import cache entry", exc_info=True)
        else:
            return value
    value = compute()
    _save(path, key, value)
    return value
