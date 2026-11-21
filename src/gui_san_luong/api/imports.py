"""Uploaded workbooks kept between API calls, the way a Streamlit session keeps them.

A frontend uploads the month's files once, then asks for days, employees and previews many
times, and may change how a file is read. Each upload is an import session held in memory
for IMPORT_TTL after its last use. The API runs as a single process, so a dict is enough;
a restart only means uploading again, as reading is cached on disk by content.
"""

import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from gui_san_luong.services.employee_roster import Roster, read_roster
from gui_san_luong.services.excel_import import ExcelImportError
from gui_san_luong.services.import_cache import cache_key, cached, file_digest
from gui_san_luong.services.production_review import (
    CombinedOutput,
    FileOutcome,
    FileReading,
    ReadSettings,
    SkippedFile,
    UploadCandidate,
    combine_outputs,
    deduplicate,
    read_workbook,
    record_changes,
)
from gui_san_luong.services.worker_output import EmployeeDay, summarize_day

IMPORT_TTL = timedelta(hours=8)
MAX_SESSIONS = 20
MAX_FILES = 20
ID_BYTES = 9


def _new_id() -> str:
    return secrets.token_urlsafe(ID_BYTES)


@dataclass(frozen=True)
class _Candidate(UploadCandidate):
    data: bytes


@dataclass
class StoredFile:
    file_id: str
    name: str
    data: bytes
    digest: str
    settings: ReadSettings = field(default_factory=ReadSettings)
    reading: FileReading | None = None
    roster: Roster | None = None


@dataclass
class ImportSession:
    import_id: str
    created_at: datetime
    last_used: datetime
    files: dict[str, StoredFile]
    skipped: list[SkippedFile]
    # File versions already compared with the stored history.
    recorded_versions: set[tuple[str, str, str]] = field(default_factory=set)
    # Rows found edited compared with the previous upload, by the last comparison.
    changed_rows: int = 0
    edited_files: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def outcomes(self) -> list[FileOutcome]:
        return [file.reading.outcome for file in self.files.values() if file.reading]

    def rosters(self) -> dict[str, Roster | None]:
        return {file.name: file.roster for file in self.files.values()}

    def combined(self) -> CombinedOutput:
        return combine_outputs(self.outcomes(), self.rosters())

    def employees(self, day: date) -> list[EmployeeDay]:
        return summarize_day(self.combined().outputs, day)

    def file(self, file_id: str) -> StoredFile | None:
        return self.files.get(file_id)


type Reader = Callable[[str, str, Callable[[], Any]], Any]


def disk_reader(cache_path: Path) -> Reader:
    def read(key: str, _message: str, compute: Callable[[], Any]) -> Any:
        return cached(cache_path, key, compute)

    return read


def read_file(stored: StoredFile, reader: Reader) -> None:
    stored.reading = read_workbook(
        stored.name, stored.data, stored.digest, stored.settings, reader
    )
    try:
        stored.roster = reader(
            cache_key("roster", stored.digest),
            "",
            lambda: read_roster(stored.data),
        )
    except ExcelImportError:
        # The file's own outcome already says it cannot be read.
        stored.roster = None


def compare_with_history(session: ImportSession, db_path: Path) -> None:
    """Raises ChangeStorageError when the history cannot be read or written."""
    combined = session.combined()
    recorded = record_changes(
        db_path, session.outcomes(), combined.outputs, session.recorded_versions
    )
    session.recorded_versions.update(recorded.versions)
    if recorded.versions:
        session.changed_rows = recorded.changed_rows
        session.edited_files = recorded.edited_files


class ImportStore:
    def __init__(
        self,
        ttl: timedelta = IMPORT_TTL,
        max_sessions: int = MAX_SESSIONS,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions: dict[str, ImportSession] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._max = max_sessions
        self._now = now

    def _expire(self) -> None:
        cutoff = self._now() - self._ttl
        for import_id, session in list(self._sessions.items()):
            if session.last_used < cutoff:
                del self._sessions[import_id]
        while len(self._sessions) >= self._max:
            oldest = min(self._sessions.values(), key=lambda s: s.last_used)
            del self._sessions[oldest.import_id]

    def create(
        self, uploads: Sequence[tuple[str, bytes]], reader: Reader
    ) -> ImportSession:
        candidates = [
            _Candidate(name, len(data), file_digest(data), data)
            for name, data in uploads
        ]
        kept, skipped = deduplicate(candidates)
        now = self._now()
        files = {}
        for candidate in kept:
            stored = StoredFile(
                _new_id(), candidate.name, candidate.data, candidate.digest
            )
            read_file(stored, reader)
            files[stored.file_id] = stored
        session = ImportSession(_new_id(), now, now, files, skipped)
        with self._lock:
            self._expire()
            self._sessions[session.import_id] = session
        return session

    def get(self, import_id: str) -> ImportSession | None:
        with self._lock:
            self._expire()
            session = self._sessions.get(import_id)
            if session is not None:
                session.last_used = self._now()
            return session

    def delete(self, import_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(import_id, None) is not None
