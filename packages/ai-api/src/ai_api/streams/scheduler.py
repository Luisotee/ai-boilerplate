"""Scheduler: fires due scheduled tasks onto the chats' own streams.

Runs as the fourth task of the stream worker (``scripts/run_stream_worker.py``).
Every ``TICK_SECONDS``:

1. **Claim due rows** with ``SELECT … FOR UPDATE SKIP LOCKED``, so several
   workers on one database never take the same task (no Redis lease needed).
2. **Advance before dispatch (at most once).** In the same commit as the
   ``task_runs`` row for the slot, ``next_run_at`` moves to the next slot (or
   NULL for a one-shot). A crash after the commit can lose a slot, never fire
   it twice; ``UNIQUE(task_id, scheduled_for)`` backs that up.
3. **No burst after downtime.** A recurring slot fires only if it is less late
   than its catch-up window (half the period, 2 min - 2 h); the next slot is
   always the first one after *now*, so N missed slots become one send or
   none. A one-shot fires up to ``ONE_SHOT_GRACE`` (2 h) late, otherwise it is
   recorded as missed and disabled, never silently lost.
4. **Dispatch**: add ``trigger=scheduled`` to ``stream:user:{chat id}`` (the
   run is serialised with that chat's messages; ``scheduled_runner.py`` runs
   it) and mark the run ``dispatched``.
5. **Orphan sweep**: ``queued`` > 5 min (the XADD never happened) → failed;
   ``dispatched`` > 1 h (stuck behind a dead stream) → failed, not sent hours
   late; ``running`` > 15 min (crashed mid-send) → ``unknown``, never re-sent.

A failing tick is logged and the loop goes on; it never takes the worker down.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from ..database import ScheduledTask, SessionLocal, TaskRun
from ..logger import logger
from ..runtime_config import runtime_config
from ..services.schedule import catchup_window, next_fire, period
from ..services.scheduled_tasks import task_timezone, utcnow
from .manager import add_message_to_stream
from .scheduled_runner import FAILED, SKIPPED, UNKNOWN, apply_outcome

TICK_SECONDS = 15.0
BATCH_SIZE = 50
QUEUED_ORPHAN_AFTER = timedelta(minutes=5)
DISPATCHED_EXPIRE_AFTER = timedelta(hours=1)
RUNNING_ORPHAN_AFTER = timedelta(minutes=15)

MISSED = "missed"
ENQUEUE_FAILED = "enqueue_failed"
EXPIRED = "expired"


@dataclass(frozen=True)
class Fired:
    run_id: uuid.UUID
    user_id: uuid.UUID


def claim_due(now: datetime) -> list[Fired]:
    """Advance every due task past ``now`` and create its run rows; returns runs to dispatch."""
    db = SessionLocal()
    fired: list[Fired] = []
    try:
        tasks = (
            db.query(ScheduledTask)
            .filter(
                ScheduledTask.status == "active",
                ScheduledTask.next_run_at.isnot(None),
                ScheduledTask.next_run_at <= now,
            )
            .order_by(ScheduledTask.next_run_at)
            .limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)
            .all()
        )
        for task in tasks:
            run = _advance(db, task, now)
            if run is not None and run.status == "queued":
                fired.append(Fired(run.id, task.user_id))
        db.commit()
        return fired
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _advance(db, task: ScheduledTask, now: datetime) -> TaskRun | None:
    """Move one locked task to its next slot and record this slot's run."""
    slot = task.next_run_at
    late = now - slot
    tz = task_timezone(task)
    recurring = task.run_at is None
    fire = late <= catchup_window(
        period(tz=tz, slot=slot, cron_expr=task.cron_expr, interval_seconds=task.interval_seconds)
        if recurring
        else None
    )
    if recurring:
        task.next_run_at = next_fire(
            tz=tz,
            after=now,
            cron_expr=task.cron_expr,
            interval_seconds=task.interval_seconds,
            previous=slot,
        )
    else:
        task.next_run_at = None

    run = TaskRun(
        id=uuid.uuid4(),
        task_id=task.id,
        user_id=task.user_id,
        scheduled_for=slot,
        status="queued" if fire else SKIPPED,
        error_code=None if fire else MISSED,
        late_seconds=max(0, int(late.total_seconds())),
        finished_at=None if fire else now,
    )
    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
    except IntegrityError:
        # This slot already has a run (another scheduler got here first, or a
        # re-armed task landed on an old slot): never fire it twice.
        logger.warning("Scheduled task %s: slot %s already ran; skipping", task.id, slot)
        return None
    if not fire:
        logger.warning(
            "Scheduled task %s: slot %s missed by %ss; skipped",
            task.id,
            slot,
            int(late.total_seconds()),
        )
        apply_outcome(task, SKIPPED, MISSED, now)
    elif late > timedelta(minutes=1):
        logger.info(
            "Scheduled task %s: firing slot %s late (%ss)", task.id, slot, int(late.total_seconds())
        )
    return run


def mark_dispatched(run_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        db.query(TaskRun).filter(TaskRun.id == run_id, TaskRun.status == "queued").update(
            {"status": "dispatched"}, synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def sweep_orphans(now: datetime) -> int:
    """Settle runs that will never finish on their own; returns how many."""
    rules = (
        ("queued", QUEUED_ORPHAN_AFTER, TaskRun.created_at, FAILED, ENQUEUE_FAILED),
        ("dispatched", DISPATCHED_EXPIRE_AFTER, TaskRun.created_at, FAILED, EXPIRED),
        ("running", RUNNING_ORPHAN_AFTER, TaskRun.started_at, UNKNOWN, UNKNOWN),
    )
    db = SessionLocal()
    settled = 0
    try:
        for status, after, column, new_status, code in rules:
            runs = (
                db.query(TaskRun)
                .filter(TaskRun.status == status, column < now - after)
                .with_for_update(skip_locked=True)
                .all()
            )
            for run in runs:
                run.status = new_status
                run.error_code = code
                run.finished_at = now
                task = db.get(ScheduledTask, run.task_id)
                if task is not None:
                    apply_outcome(task, new_status, code, now)
                logger.warning("Scheduled run %s settled as %s (%s)", run.id, new_status, code)
                settled += 1
        db.commit()
        return settled
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def tick(redis: Redis) -> int:
    """One scheduler pass; returns how many runs were dispatched."""
    now = utcnow()
    await asyncio.to_thread(sweep_orphans, now)
    if not runtime_config.get("scheduled_tasks_enabled"):
        return 0
    fired = await asyncio.to_thread(claim_due, now)
    dispatched = 0
    for item in fired:
        try:
            await add_message_to_stream(
                redis,
                str(item.user_id),
                {
                    "trigger": "scheduled",
                    "run_id": str(item.run_id),
                    "user_id": str(item.user_id),
                },
            )
        except Exception:
            # Left 'queued': the sweep records it as enqueue_failed.
            logger.error("Scheduled run %s: could not enqueue", item.run_id, exc_info=True)
            continue
        try:
            await asyncio.to_thread(mark_dispatched, item.run_id)
        except Exception:
            # Harmless: the runner claims 'queued' runs too.
            logger.warning("Scheduled run %s: could not mark dispatched", item.run_id)
        dispatched += 1
    return dispatched


async def run_scheduler(redis: Redis) -> None:
    """Worker loop: fire due scheduled tasks every ``TICK_SECONDS``."""
    logger.info("⏰ Starting scheduler")
    while True:
        try:
            await tick(redis)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Error in scheduler tick", exc_info=True)
        await asyncio.sleep(TICK_SECONDS)
