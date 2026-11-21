"""Turn service results into the API's response bodies."""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime

from gui_san_luong.api.imports import IMPORT_TTL, ImportSession, StoredFile
from gui_san_luong.api.schemas import (
    ChangeOut,
    ColumnOptionOut,
    FieldMatchOut,
    FileDetail,
    FileSummary,
    ImportOut,
    IssueOut,
    IssuesOut,
    OutputLineOut,
    RecipientPlanOut,
    RejectedRowOut,
    SheetOut,
    SkippedFileOut,
    TotalOut,
)
from gui_san_luong.services.excel_import import MatchStatus
from gui_san_luong.services.output_changes import OutputChange
from gui_san_luong.services.output_dispatch import RecipientPlan
from gui_san_luong.services.production_review import (
    Issue,
    Severity,
    column_options,
    file_status,
    preview_rows,
)
from gui_san_luong.services.worker_output import (
    EmployeeLine,
    available_days,
    format_line,
)
from gui_san_luong.services.zalo_texts import RECIPIENT_STATE_TEXTS

PREVIEW_ROW_LIMIT = 200
MATCH_STATUS = {
    MatchStatus.AUTO: "auto",
    MatchStatus.AMBIGUOUS: "ambiguous",
}


def from_epoch_ms(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, UTC)


def file_summary(stored: StoredFile) -> FileSummary:
    reading = stored.reading
    assert reading is not None
    outcome = reading.outcome
    status, text = file_status(outcome)
    result = outcome.result
    return FileSummary(
        file_id=stored.file_id,
        name=stored.name,
        status=status,
        status_text=text,
        template_key=reading.template.key if reading.template else None,
        template_label=outcome.template_label,
        sheet=outcome.sheet or (reading.sheet.name if reading.sheet else ""),
        rows_read=len(result.rows) if result else 0,
        rejected_rows=len(result.rejected) if result else 0,
        warnings=list(outcome.warnings),
        excluded_because=outcome.excluded_because,
    )


def file_detail(stored: StoredFile) -> FileDetail:
    reading = stored.reading
    assert reading is not None
    summary = file_summary(stored)
    template, layout, sheet = reading.template, reading.layout, reading.sheet
    fields = []
    if template is not None and layout is not None:
        for spec in template.fields:
            match = layout.matches[spec.key]
            fields.append(
                FieldMatchOut(
                    field_key=spec.key,
                    label=spec.label,
                    required=spec.required,
                    status=MATCH_STATUS.get(match.status, "missing"),  # type: ignore[arg-type]
                    detected_column=match.column,
                    candidates=list(match.candidates),
                    chosen_column=reading.mapping.get(spec.key),
                )
            )
    columns = (
        [
            ColumnOptionOut(
                column=option.column,
                letter=option.letter,
                header=option.header,
                sample=option.sample,
            )
            for option in column_options(sheet, layout, reading.start_row)
        ]
        if sheet is not None and layout is not None and reading.start_row
        else []
    )
    result = reading.outcome.result
    return FileDetail(
        **summary.model_dump(),
        template_suggested=reading.template_suggested,
        sheets=[
            SheetOut(name=preview.name, hidden=preview.hidden)
            for preview in reading.previews
        ],
        sheet_found=reading.sheet_found,
        start_row=reading.start_row,
        start_row_detected=reading.start_row_detected,
        fields=fields,
        columns=columns,
        extra_columns=list(reading.extras.values()),
        totals=[
            TotalOut(
                label=total.label,
                counted=total.counted,
                all_rows=total.all_rows,
                stated=total.stated,
                matches_stated=total.matches_stated,
            )
            for total in (result.totals if result else [])
        ],
        rejected=[
            RejectedRowOut(row=item.row, reason=item.reason)
            for item in (result.rejected if result else [])
        ],
        blank_rows=result.blank_rows if result else 0,
        total_rows=result.total_rows if result else 0,
        repeated_rows=result.repeated_rows if result else 0,
        groups=list(result.groups) if result else [],
        preview_rows=(
            preview_rows(result, template, list(reading.extras), PREVIEW_ROW_LIMIT)
            if result and template
            else []
        ),
    )


def import_out(session: ImportSession) -> ImportOut:
    return ImportOut(
        import_id=session.import_id,
        created_at=session.created_at,
        expires_after_minutes=int(IMPORT_TTL.total_seconds() // 60),
        files=[file_summary(stored) for stored in session.files.values()],
        skipped=[
            SkippedFileOut(name=item.name, reason=item.reason) for item in session.skipped
        ],
        days=available_days(session.combined().outputs),
        changed_rows=session.changed_rows,
        edited_files=list(session.edited_files),
    )


def issues_out(issues: Sequence[Issue], day: date | None) -> IssuesOut:
    return IssuesOut(
        day=day,
        errors=sum(issue.severity is Severity.ERROR for issue in issues),
        warnings=sum(issue.severity is Severity.WARNING for issue in issues),
        issues=[
            IssueOut(
                severity=issue.severity,
                title=issue.title,
                detail=issue.detail,
                rows=[dict(row) for row in issue.rows],
            )
            for issue in issues
        ],
    )


def line_out(line: EmployeeLine) -> OutputLineOut:
    return OutputLineOut(
        department=line.department,
        role=line.role,
        process=line.process,
        quantity=line.quantity,
        unit=line.unit,
        text=format_line(line),
    )


def change_out(change: OutputChange) -> ChangeOut:
    return ChangeOut(
        id=change.id,
        detected_at=change.detected_at,
        file_name=change.file_name,
        day=change.day,
        employee_code=change.employee_code,
        employee_name=change.employee_name,
        line=change.line,
        unit=change.unit,
        old_quantity=change.old_quantity,
        new_quantity=change.new_quantity,
        difference=change.difference,
        excel_row=change.excel_row,
    )


def plan_out(plan: RecipientPlan) -> RecipientPlanOut:
    return RecipientPlanOut(
        employee_code=plan.employee.employee_code,
        employee_name=plan.employee.employee_name,
        zalo_user_id=plan.zalo_user_id,
        state=plan.state,
        state_text=RECIPIENT_STATE_TEXTS[plan.state],
        window=plan.window.value if plan.window else None,  # type: ignore[arg-type]
        last_interaction_at=(
            from_epoch_ms(plan.last_interaction_ms)
            if plan.last_interaction_ms is not None
            else None
        ),
        parts=len(plan.messages),
        pending_parts=list(plan.pending_parts),
    )


def changes_by_employee(
    changes: Iterable[OutputChange], day: date
) -> Mapping[str, list[OutputChange]]:
    found: dict[str, list[OutputChange]] = {}
    for change in changes:
        if change.day == day:
            found.setdefault(change.employee_code, []).append(change)
    return found
