"""Scheduled tasks: list, inspect, re-enable, cancel and delete them (operator view).

FleetView is the intended caller. Routes live under ``/admin/scheduled-tasks``
behind the standard ``X-API-Key`` middleware. Users create and cancel their own
tasks through the ``manage_scheduled_tasks`` agent tool; this is where a task
the scheduler disabled (repeated failures, a missed one-shot) shows up, with
its recent runs.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import ScheduledTask, TaskRun, User, get_db
from ..deps import limiter
from ..logger import logger
from ..runtime_config import runtime_config
from ..schemas import (
    ScheduledTaskDetail,
    ScheduledTaskKind,
    ScheduledTasksResponse,
    ScheduledTaskStatus,
    ScheduledTaskSummary,
    ScheduledTaskUpdateRequest,
    TaskRunItem,
)
from ..services.scheduled_tasks import STATUSES, active_count, rearm, utcnow

router = APIRouter(prefix="/admin/scheduled-tasks", tags=["Admin", "Scheduled tasks"])

#: Runs shown on the detail route.
DETAIL_RUNS = 20


def _summary_fields(task: ScheduledTask, user: User | None) -> dict:
    return {
        "id": str(task.id),
        "status": task.status,
        "kind": task.kind,
        "text": task.text,
        "schedule": task.schedule,
        "timezone": task.timezone,
        "whatsapp_jid": task.address,
        "user_name": user.name if user else None,
        "client_id": task.client_id,
        "conversation_type": user.conversation_type if user else None,
        "next_run_at": task.next_run_at,
        "last_run_at": task.last_run_at,
        "last_status": task.last_status,
        "last_error": task.last_error,
        "disabled_reason": task.disabled_reason,
        "consecutive_failures": task.consecutive_failures or 0,
        "run_count": task.run_count or 0,
        "created_at": task.created_at,
    }


def _get_or_404(db: Session, task_id: uuid.UUID) -> tuple[ScheduledTask, User | None]:
    row = (
        db.query(ScheduledTask, User)
        .outerjoin(User, User.id == ScheduledTask.user_id)
        .filter(ScheduledTask.id == task_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return row[0], row[1]


def _detail(db: Session, task: ScheduledTask, user: User | None) -> ScheduledTaskDetail:
    runs = (
        db.query(TaskRun)
        .filter(TaskRun.task_id == task.id)
        .order_by(TaskRun.scheduled_for.desc())
        .limit(DETAIL_RUNS)
        .all()
    )
    return ScheduledTaskDetail(
        **_summary_fields(task, user),
        cron_expr=task.cron_expr,
        interval_seconds=task.interval_seconds,
        run_at=task.run_at,
        runs=[
            TaskRunItem(
                id=str(r.id),
                scheduled_for=r.scheduled_for,
                status=r.status,
                error_code=r.error_code,
                late_seconds=r.late_seconds or 0,
                created_at=r.created_at,
                started_at=r.started_at,
                finished_at=r.finished_at,
            )
            for r in runs
        ],
    )


@router.get("", response_model=ScheduledTasksResponse)
async def list_scheduled_tasks(
    status: ScheduledTaskStatus | None = None,
    kind: ScheduledTaskKind | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Tasks, most recently changed first, optionally filtered by status / kind."""
    query = db.query(ScheduledTask, User).outerjoin(User, User.id == ScheduledTask.user_id)
    if status:
        query = query.filter(ScheduledTask.status == status)
    if kind:
        query = query.filter(ScheduledTask.kind == kind)
    total = query.count()
    rows = (
        query.order_by(ScheduledTask.updated_at.desc(), ScheduledTask.id)
        .limit(limit)
        .offset(offset)
        .all()
    )
    counts = dict.fromkeys(STATUSES, 0)
    count_query = db.query(ScheduledTask.status, func.count(ScheduledTask.id))
    if kind:
        count_query = count_query.filter(ScheduledTask.kind == kind)
    for value, n in count_query.group_by(ScheduledTask.status):
        if value in counts:
            counts[value] = n
    return ScheduledTasksResponse(
        tasks=[ScheduledTaskSummary(**_summary_fields(t, u)) for t, u in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts_by_status=counts,
    )


@router.get("/{task_id}", response_model=ScheduledTaskDetail)
@limiter.exempt
async def get_scheduled_task(request: Request, task_id: uuid.UUID, db: Session = Depends(get_db)):
    """One task with its latest runs."""
    task, user = _get_or_404(db, task_id)
    return _detail(db, task, user)


@router.patch("/{task_id}", response_model=ScheduledTaskDetail)
async def update_scheduled_task(
    task_id: uuid.UUID, body: ScheduledTaskUpdateRequest, db: Session = Depends(get_db)
):
    """Re-enable (``active``) or stop (``cancelled``) a task.

    Re-enabling resets the failure count and arms the next slot after now; a
    one-shot whose time has passed, or a task in a chat already at
    ``scheduled_tasks_max_per_chat`` active tasks, can't be re-enabled (409).
    Completed and cancelled tasks stay as they are (409).
    """
    task, user = _get_or_404(db, task_id)
    if task.status == body.status:
        return _detail(db, task, user)
    if task.status in ("completed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Task is already {task.status}")
    now = utcnow()
    try:
        if body.status == "active":
            next_run = rearm(task, now)
            if next_run is None:
                raise HTTPException(status_code=409, detail="This one-time task's time has passed")
            max_tasks = runtime_config.get("scheduled_tasks_max_per_chat")
            if active_count(db, task.user_id) >= max_tasks:
                raise HTTPException(
                    status_code=409,
                    detail=f"This chat already has {max_tasks} active scheduled tasks",
                )
            task.status = "active"
            task.disabled_reason = None
            task.consecutive_failures = 0
            task.next_run_at = next_run
        else:
            task.status = "cancelled"
            task.next_run_at = None
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.error(f"Failed to update scheduled task {task_id}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    return _detail(db, task, user)


@router.delete("/{task_id}", status_code=204)
async def delete_scheduled_task(task_id: uuid.UUID, db: Session = Depends(get_db)):
    """Delete a task and its run history for good."""
    task, _ = _get_or_404(db, task_id)
    try:
        db.delete(task)
        db.commit()
    except Exception:
        db.rollback()
        logger.error(f"Failed to delete scheduled task {task_id}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from None
    return Response(status_code=204)
