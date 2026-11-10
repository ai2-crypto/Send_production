"""Print the structure of Excel workbooks without modifying them or running macros."""

import argparse
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

MISSING_CACHE = "<chưa có giá trị>"
MAX_CELL_WIDTH = 40
SUPPORTED_SUFFIXES = {".xlsx", ".xlsm"}


def format_cell(formula: object, cached: object) -> str:
    is_formula = isinstance(formula, str) and formula.startswith("=")
    if not is_formula:
        return "" if cached is None else repr(cached)[:MAX_CELL_WIDTH]
    shown = MISSING_CACHE if cached is None else repr(cached)
    return f"{shown} [{formula}]"[:MAX_CELL_WIDTH]


def describe_sheet(formulas: Worksheet, values: Worksheet, max_rows: int) -> None:
    hidden_rows = sum(1 for d in formulas.row_dimensions.values() if d.hidden)
    hidden_columns = sum(1 for d in formulas.column_dimensions.values() if d.hidden)
    formula_count = 0
    uncached_count = 0
    for row in formulas.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                formula_count += 1
                uncached_count += values[cell.coordinate].value is None

    print(f"\n## Sheet {formulas.title!r} ({formulas.sheet_state})")
    print(f"Vùng dữ liệu: {formulas.dimensions}")
    print(f"Ô gộp: {[str(r) for r in formulas.merged_cells.ranges]}")
    print(f"Dòng ẩn: {hidden_rows}; cột ẩn: {hidden_columns}")
    print(f"Công thức: {formula_count}; chưa có giá trị: {uncached_count}")

    # iter_rows pads past the used range, which would print rows that do not exist.
    last_row = min(max_rows, formulas.max_row)
    rows = zip(
        formulas.iter_rows(max_row=last_row),
        values.iter_rows(max_row=last_row),
        strict=True,
    )
    for formula_row, value_row in rows:
        cells = [format_cell(f.value, v.value) for f, v in zip(formula_row, value_row, strict=True)]
        print(f"{formula_row[0].row:>5} | " + " | ".join(cells))


def inspect_workbook(path: Path, max_rows: int) -> None:
    print(f"\n# {path.name}")
    formula_book = load_workbook(path, data_only=False)
    value_book = load_workbook(path, data_only=True)
    try:
        for name in formula_book.sheetnames:
            describe_sheet(formula_book[name], value_book[name], max_rows)
    finally:
        formula_book.close()
        value_book.close()


def main() -> int:
    # Windows consoles may default to a code page that cannot print Vietnamese.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--rows", type=int, default=15, help="số dòng đầu cần in cho mỗi sheet")
    args = parser.parse_args()

    exit_code = 0
    for path in args.paths:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            print(f"Bỏ qua {path.name}: chỉ hỗ trợ .xlsx và .xlsm", file=sys.stderr)
            exit_code = 1
            continue
        inspect_workbook(path, args.rows)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
