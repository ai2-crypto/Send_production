"""Reusing what was read from a workbook, using a throwaway cache file."""

import sqlite3
from pathlib import Path

import pytest

from gui_san_luong.services.import_cache import cache_key, cached, file_digest


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict[str, int]:
        self.calls += 1
        return {"rows": self.calls}


def test_a_value_is_computed_once_then_reused(tmp_path: Path) -> None:
    path, compute = tmp_path / "cache.db", Counter()
    key = cache_key("rows", file_digest(b"workbook"), ("Sheet1", 5))

    assert cached(path, key, compute) == {"rows": 1}
    assert cached(path, key, compute) == {"rows": 1}
    assert compute.calls == 1


def test_other_content_or_settings_are_read_afresh(tmp_path: Path) -> None:
    path, compute = tmp_path / "cache.db", Counter()
    digest = file_digest(b"workbook")
    cached(path, cache_key("rows", digest, ("Sheet1", 5)), compute)

    cached(path, cache_key("rows", digest, ("Sheet1", 6)), compute)
    cached(path, cache_key("rows", file_digest(b"edited"), ("Sheet1", 5)), compute)
    cached(path, cache_key("roster", digest), compute)

    assert compute.calls == 4


def test_an_unusable_entry_is_read_again(tmp_path: Path) -> None:
    path, compute = tmp_path / "cache.db", Counter()
    key = cache_key("rows", "digest")
    cached(path, key, compute)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE import_cache SET value = ?", (b"not a pickle",))
    connection.close()

    assert cached(path, key, compute) == {"rows": 2}


def test_a_broken_cache_file_does_not_stop_reading(tmp_path: Path) -> None:
    path = tmp_path / "cache.db"
    path.write_bytes(b"this is not a database")

    assert cached(path, cache_key("rows", "digest"), Counter()) == {"rows": 1}


def test_errors_are_not_stored(tmp_path: Path) -> None:
    path, key = tmp_path / "cache.db", cache_key("rows", "digest")

    def fail() -> int:
        raise ValueError("unreadable")

    with pytest.raises(ValueError):
        cached(path, key, fail)
    assert cached(path, key, lambda: 7) == 7
