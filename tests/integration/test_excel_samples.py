"""Check real workbooks against totals stated inside them; skipped where files are absent."""

import warnings
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from gui_san_luong.domain.production_templates import TEMPLATES, ReportTemplate
from gui_san_luong.services.excel_import import (
    ImportResult,
    MatchStatus,
    analyze_sheet,
    read_previews,
    read_rows,
    suggest_sheet,
    suggest_template,
)
from gui_san_luong.services.worker_output import credit_workers

SAMPLES = Path(__file__).parents[2] / "data" / "excel_samples"
INSERTED_COLUMNS = 2


def _sample_files() -> list[Path]:
    files = []
    for path in sorted(SAMPLES.glob("*.xlsx")):
        if not path.name.startswith("~$") and suggest_template(path.name, TEMPLATES):
            files.append(path)
    return files


def _import(data: bytes, template: ReportTemplate) -> tuple[str, ImportResult]:
    previews = read_previews(data)
    sheet = suggest_sheet(previews, template)
    assert sheet is not None
    preview = next(item for item in previews if item.name == sheet)
    layout = analyze_sheet(preview, template)
    assert layout.data_start_row is not None
    mapping = {
        key: match.column
        for key, match in layout.matches.items()
        if match.status is MatchStatus.AUTO and match.column is not None
    }
    return sheet, read_rows(data, sheet, template, mapping, layout.data_start_row)


def _copy_with_inserted_columns(source: Path, sheet: str, target: Path) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        original = load_workbook(source, read_only=True, data_only=True)
    copy = Workbook()
    copied = copy.active
    assert copied is not None
    for row in original[sheet].iter_rows(values_only=True):
        copied.append((None, *[None] * (INSERTED_COLUMNS - 1), *row))
    original.close()
    copy.save(target)
    return target.read_bytes()


@pytest.fixture(
    params=_sample_files() or [None], ids=lambda path: path.name if path else "none"
)
def sample(request: pytest.FixtureRequest) -> Path:
    if request.param is None:
        pytest.skip("Không có file trong data/excel_samples")
    path: Path = request.param
    return path


def _template_for(path: Path) -> ReportTemplate:
    template = suggest_template(path.name, TEMPLATES)
    assert template is not None
    return template


def test_real_file_matches_its_own_totals(sample: Path) -> None:
    _, result = _import(sample.read_bytes(), _template_for(sample))
    assert not result.rejected
    assert result.rows
    mismatched = [
        total.label for total in result.totals if total.matches_stated is False
    ]
    assert not mismatched
    assert any(total.matches_stated for total in result.totals)


def test_real_file_reads_the_same_after_columns_shift(
    sample: Path, tmp_path: Path
) -> None:
    template = _template_for(sample)
    sheet, baseline = _import(sample.read_bytes(), template)
    shifted = _copy_with_inserted_columns(sample, sheet, tmp_path / "shifted.xlsx")
    _, moved = _import(shifted, template)
    assert [t.counted for t in moved.totals] == pytest.approx(
        [t.counted for t in baseline.totals]
    )
    assert len(moved.rows) == len(baseline.rows)


def test_real_file_credits_every_share_column_to_employees(sample: Path) -> None:
    template = _template_for(sample)
    data = sample.read_bytes()
    previews = read_previews(data)
    sheet = suggest_sheet(previews, template)
    assert sheet is not None
    layout = analyze_sheet(
        next(item for item in previews if item.name == sheet), template
    )
    assert layout.data_start_row is not None
    mapping = {
        key: match.column
        for key, match in layout.matches.items()
        if match.status is MatchStatus.AUTO and match.column is not None
    }
    result = read_rows(data, sheet, template, mapping, layout.data_start_row)
    credited = credit_workers(result, template, mapping, sample.name)
    assert credited.outputs
    assert all(item.employee_code.casefold() != "cty" for item in credited.outputs)
    if template.detail.item_fields:
        assert all(item.detail.item for item in credited.outputs)
    if template.detail.step_fields:
        assert any(item.detail.step for item in credited.outputs)
    if template.detail.machine_fields:
        assert any(item.detail.machine for item in credited.outputs)
    if template.detail.shift_fields:
        assert any(item.detail.shift for item in credited.outputs)

    # Shares must reach the file's own totals, apart from those reported as unassignable.
    shares = {
        slot.quantity_field
        for slot in template.worker_slots
        if slot.quantity_field != template.quantity_field
    }
    for total in result.totals:
        if total.field_key in shares:
            assert total.matches_stated is True, total.label

    # Coefficients split a machine's output, so together the people get exactly that output.
    if any(slot.ratio_field for slot in template.worker_slots):
        output = next(
            total
            for total in result.totals
            if total.field_key == template.quantity_field
        )
        assert sum(item.quantity for item in credited.outputs) == pytest.approx(
            output.counted
        )
