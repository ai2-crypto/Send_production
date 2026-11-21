"""Settings come from the environment first, then from the .env file."""

from pathlib import Path

import pytest

from gui_san_luong.config import read_dotenv, setting

NAME = "GUI_SAN_LUONG_TEST_SETTING"


def test_dotenv_lines_are_read_with_comments_quotes_and_export(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# comment\n\nA=1\nexport B = two \nC=\"quoted value\"\nD='x=y'\nnot a setting\n",
        encoding="utf-8",
    )

    assert read_dotenv(dotenv) == {
        "A": "1",
        "B": "two",
        "C": "quoted value",
        "D": "x=y",
    }


def test_a_missing_dotenv_gives_nothing(tmp_path: Path) -> None:
    assert read_dotenv(tmp_path / "missing") == {}


def test_the_environment_wins_over_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{NAME}=from-file\n", encoding="utf-8")
    monkeypatch.delenv(NAME, raising=False)
    assert setting(NAME, dotenv) == "from-file"

    monkeypatch.setenv(NAME, "from-env")
    assert setting(NAME, dotenv) == "from-env"


def test_an_edited_dotenv_applies_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(NAME, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{NAME}=old\n", encoding="utf-8")
    assert setting(NAME, dotenv) == "old"

    dotenv.write_text(f"{NAME}=new\n", encoding="utf-8")
    assert setting(NAME, dotenv) == "new"
