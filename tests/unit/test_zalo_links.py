"""Linking employees to Zalo accounts, using a throwaway database."""

import sqlite3
from pathlib import Path

import pytest

from gui_san_luong.db.connection import open_database
from gui_san_luong.db.zalo_links import LinkRecord
from gui_san_luong.services.worker_output import EmployeeIdentity
from gui_san_luong.services.zalo_links import (
    InvalidLinksError,
    LinkRow,
    build_editable_rows,
    load_links,
    plan_changes,
    save_links,
)

ACTOR = "tester"
ZALO_A = "2512523625412515"
ZALO_B = "2512523625412516"


def _problems(*rows: LinkRow) -> list[str]:
    with pytest.raises(InvalidLinksError) as error:
        plan_changes([], rows)
    return error.value.problems


def _audit(path: Path) -> list[tuple[str, str, str]]:
    with open_database(path) as connection:
        return list(
            connection.execute(
                "SELECT actor, action, target FROM audit_log ORDER BY id"
            )
        )


def test_new_link_is_planned_and_blank_ids_are_not() -> None:
    changes = plan_changes(
        [], [LinkRow("10", "An", f" {ZALO_A} "), LinkRow("20", "Bo", "")]
    )
    assert changes.upserts == [LinkRecord("10", "An", ZALO_A)]
    assert not changes.removals


def test_unchanged_table_plans_nothing() -> None:
    stored = [LinkRecord("10", "An", ZALO_A)]
    assert plan_changes(stored, [LinkRow("10", "An", ZALO_A)]).is_empty


def test_clearing_an_id_or_deleting_the_row_removes_the_link() -> None:
    stored = [LinkRecord("10", "An", ZALO_A), LinkRecord("20", "Bo", ZALO_B)]
    changes = plan_changes(stored, [LinkRow("10", "An", "")])
    assert changes.removals == ["10", "20"]


def test_blank_new_row_is_ignored() -> None:
    assert plan_changes([], [LinkRow(None, None, None)]).is_empty


def test_id_is_kept_as_text_with_all_digits() -> None:
    link = plan_changes([], [LinkRow("10", "An", "0012345678901234567890")]).upserts[0]
    assert link.zalo_user_id == "0012345678901234567890"


@pytest.mark.parametrize("bad_id", ["abc", "12 34", "1.25E+15", "-5"])
def test_non_digit_id_is_refused(bad_id: str) -> None:
    assert "chỉ được gồm chữ số" in _problems(LinkRow("10", "An", bad_id))[0]


def test_id_without_employee_code_is_refused() -> None:
    assert "chưa có mã nhân viên" in _problems(LinkRow("", "An", ZALO_A))[0]


def test_same_code_twice_is_refused() -> None:
    problems = _problems(LinkRow("10", "An", ZALO_A), LinkRow("10", "An", ZALO_B))
    assert "nhiều dòng" in problems[0]


def test_two_people_cannot_share_an_account() -> None:
    problems = _problems(LinkRow("10", "An", ZALO_A), LinkRow("20", "Bo", ZALO_A))
    assert "dùng chung" in problems[0]


def test_one_bad_row_saves_nothing(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    with pytest.raises(InvalidLinksError):
        save_links(
            path, [LinkRow("10", "An", ZALO_A), LinkRow("20", "Bo", "abc")], ACTOR
        )
    assert load_links(path) == []
    assert _audit(path) == []


def test_save_persists_and_writes_audit(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "test.db"
    save_links(path, [LinkRow("10", "An", ZALO_A)], ACTOR)
    assert load_links(path) == [LinkRecord("10", "An", ZALO_A)]
    save_links(path, [LinkRow("10", "An", "")], ACTOR)
    assert load_links(path) == []
    assert _audit(path) == [
        (ACTOR, "zalo_link_set", "10"),
        (ACTOR, "zalo_link_removed", "10"),
    ]


def test_two_people_can_swap_accounts(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    save_links(path, [LinkRow("10", "An", ZALO_A), LinkRow("20", "Bo", ZALO_B)], ACTOR)
    save_links(path, [LinkRow("10", "An", ZALO_B), LinkRow("20", "Bo", ZALO_A)], ACTOR)
    assert [link.zalo_user_id for link in load_links(path)] == [ZALO_B, ZALO_A]


def test_database_refuses_shared_account_even_past_the_service(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    save_links(path, [LinkRow("10", "An", ZALO_A)], ACTOR)
    with open_database(path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO employee_zalo_links VALUES ('20', 'Bo', ?, 'x', 'y')",
            (ZALO_A,),
        )


def test_reopening_does_not_rerun_migrations(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    save_links(path, [LinkRow("10", "An", ZALO_A)], ACTOR)
    assert load_links(path) == [LinkRecord("10", "An", ZALO_A)]
    with open_database(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
            0
        ] == len(list((Path(__file__).parents[2] / "migrations").glob("*.sql")))


def test_editable_rows_merge_imported_people_and_saved_links() -> None:
    rows = build_editable_rows(
        [LinkRecord("10", "An cũ", ZALO_A), LinkRecord("30", "Cư", ZALO_B)],
        [
            EmployeeIdentity("10", "Trần Văn An", ("Dệt", "May")),
            EmployeeIdentity("20", "Bo", ("In",)),
        ],
    )
    assert [
        (row.employee_code, row.employee_name, row.zalo_user_id, row.departments)
        for row in rows
    ] == [
        ("10", "Trần Văn An", ZALO_A, "Dệt, May"),
        ("20", "Bo", "", "In"),
        ("30", "Cư", ZALO_B, ""),
    ]
