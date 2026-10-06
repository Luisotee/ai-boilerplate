"""Bug reports: list, read and triage what ``services/bug_reports.py`` recorded.

FleetView is the intended caller. Routes live under ``/admin/bug-reports`` and
are protected by the standard ``X-API-Key`` middleware like the rest of
``/admin``. A report's ``context`` (the chat's last messages) and
``error_detail`` (traceback) only appear on the detail route.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import BugReport, User, get_db
from ..deps import limiter
from ..logger import logger
from ..schemas import (
    BugReportDetail,
    BugReportSource,
    BugReportsResponse,
    BugReportStatus,
    BugReportSummary,
    BugReportUpdateRequest,
)

router = APIRouter(prefix="/admin/bug-reports", tags=["Admin", "Bug reports"])

_STATUSES: tuple[str, ...] = ("open", "resolved", "ignored")


def _summary_fields(report: BugReport, user_name: str | None) -> dict:
    return {
        "id": str(report.id),
        "source": report.source,
        "status": report.status,
        "category": report.category,
        "title": report.title,
        "whatsapp_jid": report.whatsapp_jid,
        "user_name": user_name,
        "client_id": report.client_id,
        "conversation_type": report.conversation_type,
        "error_type": report.error_type,
        "occurrences": report.occurrences,
        "created_at": report.created_at,
        "last_seen_at": report.last_seen_at,
        "resolved_at": report.resolved_at,
    }


def _detail(report: BugReport, user_name: str | None) -> BugReportDetail:
    return BugReportDetail(
        **_summary_fields(report, user_name),
        description=report.description or "",
        job_id=report.job_id,
        document_id=report.document_id,
        models=report.models,
        error_detail=report.error_detail,
        context=report.context,
        resolution_note=report.resolution_note,
    )


def _get_or_404(db: Session, report_id: uuid.UUID) -> tuple[BugReport, str | None]:
    row = (
        db.query(BugReport, User.name)
        .outerjoin(User, User.id == BugReport.user_id)
        .filter(BugReport.id == report_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Bug report not found")
    return row[0], row[1]


@router.get("", response_model=BugReportsResponse)
async def list_bug_reports(
    status: BugReportStatus | None = None,
    source: BugReportSource | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Reports, most recently seen first, optionally filtered by status / source."""
    query = db.query(BugReport, User.name).outerjoin(User, User.id == BugReport.user_id)
    if status:
        query = query.filter(BugReport.status == status)
    if source:
        query = query.filter(BugReport.source == source)
    total = query.count()
    rows = (
        query.order_by(BugReport.last_seen_at.desc(), BugReport.id)
        .limit(limit)
        .offset(offset)
        .all()
    )
    counts = dict.fromkeys(_STATUSES, 0)
    for value, n in db.query(BugReport.status, func.count(BugReport.id)).group_by(BugReport.status):
        if value in counts:
            counts[value] = n
    return BugReportsResponse(
        reports=[BugReportSummary(**_summary_fields(r, name)) for r, name in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts_by_status=counts,
    )


@router.get("/{report_id}", response_model=BugReportDetail)
@limiter.exempt
async def get_bug_report(request: Request, report_id: uuid.UUID, db: Session = Depends(get_db)):
    """One report with its description, error detail and chat context."""
    return _detail(*_get_or_404(db, report_id))


@router.patch("/{report_id}", response_model=BugReportDetail)
async def update_bug_report(
    report_id: uuid.UUID, body: BugReportUpdateRequest, db: Session = Depends(get_db)
):
    """Triage a report: open / resolved / ignored, with an optional note.

    ``resolved_at`` is set when it becomes resolved and cleared otherwise.
    Reopening makes it absorb repeats of its fingerprint again.
    """
    report, user_name = _get_or_404(db, report_id)
    try:
        report.status = body.status
        report.resolved_at = (
            datetime.now(UTC).replace(tzinfo=None) if body.status == "resolved" else None
        )
        if body.resolution_note is not None:
            report.resolution_note = body.resolution_note
        db.commit()
    except Exception:
        db.rollback()
        logger.error(f"Failed to update bug report {report_id}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    return _detail(report, user_name)


@router.delete("/{report_id}", status_code=204)
async def delete_bug_report(report_id: uuid.UUID, db: Session = Depends(get_db)):
    """Delete a report for good."""
    report, _ = _get_or_404(db, report_id)
    try:
        db.delete(report)
        db.commit()
    except Exception:
        db.rollback()
        logger.error(f"Failed to delete bug report {report_id}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    return Response(status_code=204)
