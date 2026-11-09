"""Scheduler: fires due scheduled tasks.

Runs as the fourth task of the stream worker (``scripts/run_stream_worker.py``).
Every ``TICK_SECONDS``:

1. **Claim due rows** with ``SELECT … FOR UPDATE SKIP LOCKED``, so several
   workers on one database never take the same task (no Redis lease needed).
   A full batch is followed at once by the next (up to ``MAX_BATCHES_PER_TICK``).
2. **Advance before dispatch (at most once).** In the same commit as the
   ``task_runs`` row for the slot, ``next_run_at`` moves to the next slot (or
   NULL for a one-shot). A crash after the commit can lose a slot, never fire
   it twice; ``UNIQUE(task_id, scheduled_for)`` backs that up. Each task
   advances in its own savepoint: one that raises is disabled
   (``advance_error``) instead of stalling the batch on every tick.
3. **No burst after downtime.** A recurring slot fires only if it is less late
   than its catch-up window (half the period, 2 min - 2 h); the next slot is
   always the first one after *now*, so N missed slots become one send or
   none. A one-shot fires up to ``ONE_SHOT_GRACE`` (2 h) late, otherwise it is
   recorded as missed and disabled, never silently lost.
4. **Hand over.** A reminder's run is created ``ready`` with its text, for
   ``scheduled_sender.py``. An agent task's run is created ``dispatched`` and a
   ``trigger=scheduled`` entry goes on the chat's own stream (one Redis
   pipeline per batch), where ``scheduled_runner.py`` runs the agent. An XADD
   that Redis answered with an error is recorded ``failed/enqueue_failed`` at
   once; if the pipeline itself fails (connection lost part-way, so some
   entries may be in), the runs stay ``dispatched`` for the sweep.
5. **Orphan sweep**: ``dispatched`` > 1 h (stuck behind a busy or dead
   stream) and ``ready`` > 1 h (a backlog, or no sender) → ``skipped/expired``,
   not sent hours late and not counted as a failure; ``running`` > 15 min
   (crashed in the agent run, nothing sent) → failed; ``sending`` > 15 min
   (crashed mid-send) → ``unknown``, never re-sent.

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
from .scheduled_runner import (
    DISPATCHED,
    FAILED,
    READY,
    REMINDER_PREFIX,
    RUNNING,
    SENDING,
    SKIPPED,
    UNKNOWN,
    apply_outcome,
    settle,
)

TICK_SECONDS = 15.0
BATCH_SIZE = 50
MAX_BATCHES_PER_TICK = 10
STREAM_MAXLEN = 1000  # same cap as streams/manager.py add_message_to_stream
DISPATCHED_EXPIRE_AFTER = timedelta(hours=1)
READY_EXPIRE_AFTER = timedelta(hours=1)
RUNNING_ORPHAN_AFTER = timedelta(minutes=15)
SENDING_ORPHAN_AFTER = timedelta(minutes=15)

MISSED = "missed"
ENQUEUE_FAILED = "enqueue_failed"
EXPIRED = "expired"
ABANDONED = "abandoned"
ADVANCE_ERROR = "advance_error"
NO_NEXT_SLOT = "no_next_slot"


@dataclass(frozen=True)
class Fired:
    """An agent run to put on its chat's stream."""

    run_id: uuid.UUID
    user_id: uuid.UUID


def claim_due(now: datetime) -> tuple[list[Fired], int]:
    """Advance every due task past ``now`` and create its run rows.

    Returns the agent runs to dispatch and how many tasks were claimed.
    """
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
            try:
                with db.begin_nested():
                    run = _advance(db, task, now)
            except Exception:
                logger.error(
                    "Scheduled task %s: could not advance; disabling", task.id, exc_info=True
                )
                db.refresh(task)
                task.status = "disabled"
                task.disabled_reason = ADVANCE_ERROR
                task.next_run_at = None
                continue
            if run is not None and run.status == DISPATCHED:
                fired.append(Fired(run.id, task.user_id))
        db.commit()
        return fired, len(tasks)
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
        if task.next_run_at is None:
            # A cron with no future slot (parse_schedule refuses those).
            task.status = "disabled"
            task.disabled_reason = NO_NEXT_SLOT
    else:
        task.next_run_at = None

    if not fire:
        status = SKIPPED
    elif task.kind == "agent":
        status = DISPATCHED
    else:
        status = READY
    run = TaskRun(
        id=uuid.uuid4(),
        task_id=task.id,
        user_id=task.user_id,
        scheduled_for=slot,
        status=status,
        message=REMINDER_PREFIX + task.text if status == READY else None,
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


def fail_dispatched(run_ids: list[uuid.UUID], code: str) -> None:
    """Record runs whose stream entry was never added (only those still ``dispatched``)."""
    now = utcnow()
    db = SessionLocal()
    try:
        runs = (
            db.query(TaskRun)
            .filter(TaskRun.id.in_(run_ids), TaskRun.status == DISPATCHED)
            .with_for_update(skip_locked=True)
            .all()
        )
        for run in runs:
            settle(db, run, db.get(ScheduledTask, run.task_id), FAILED, code, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sweep_orphans(now: datetime) -> int:
    """Settle runs that will never finish on their own; returns how many."""
    rules = (
        # A backlog (a busy chat stream, the paced Baileys lane) is the bot's
        # own lateness, not a broken task: a skip, which never counts.
        (DISPATCHED, DISPATCHED_EXPIRE_AFTER, TaskRun.created_at, SKIPPED, EXPIRED),
        (READY, READY_EXPIRE_AFTER, TaskRun.created_at, SKIPPED, EXPIRED),
        (RUNNING, RUNNING_ORPHAN_AFTER, TaskRun.started_at, FAILED, ABANDONED),
        (SENDING, SENDING_ORPHAN_AFTER, TaskRun.started_at, UNKNOWN, UNKNOWN),
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
                settle(db, run, db.get(ScheduledTask, run.task_id), new_status, code, now)
                logger.warning("Scheduled run %s settled as %s (%s)", run.id, new_status, code)
                settled += 1
        db.commit()
        return settled
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def dispatch(redis: Redis, fired: list[Fired]) -> int:
    """Put agent runs on their chats' streams in one pipeline; returns how many went in.

    Only a command Redis answered with an error is known not to be enqueued:
    that run is failed at once. When the pipeline itself fails (e.g. the
    connection drops part-way), some entries may be in Redis, so nothing is
    failed: the runs stay ``dispatched``, the consumer claims the ones that
    arrived, and the sweep expires the rest (a skip, not a failure).
    """
    if not fired:
        return 0
    try:
        async with redis.pipeline(transaction=False) as pipe:
            for item in fired:
                pipe.xadd(
                    f"stream:user:{item.user_id}",
                    {
                        "trigger": "scheduled",
                        "run_id": str(item.run_id),
                        "user_id": str(item.user_id),
                    },
                    maxlen=STREAM_MAXLEN,
                )
            results = await pipe.execute(raise_on_error=False)
    except Exception:
        logger.error(
            "Scheduled runs: pipeline of %d agent runs failed; left for the sweep",
            len(fired),
            exc_info=True,
        )
        return 0
    failed = [
        item.run_id for item, res in zip(fired, results, strict=False) if isinstance(res, Exception)
    ]
    if failed:
        logger.error("Scheduled runs: %d of %d XADDs failed", len(failed), len(fired))
        try:
            await asyncio.to_thread(fail_dispatched, failed, ENQUEUE_FAILED)
        except Exception:
            # Left 'dispatched': the sweep expires them after an hour.
            logger.error("Scheduled runs: could not record the enqueue failure", exc_info=True)
    return len(fired) - len(failed)


async def tick(redis: Redis) -> int:
    """One scheduler pass; returns how many agent runs were put on a stream."""
    now = utcnow()
    await asyncio.to_thread(sweep_orphans, now)
    if not runtime_config.get("scheduled_tasks_enabled"):
        return 0
    dispatched = 0
    for _ in range(MAX_BATCHES_PER_TICK):
        fired, claimed = await asyncio.to_thread(claim_due, now)
        dispatched += await dispatch(redis, fired)
        if claimed < BATCH_SIZE:
            break
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
