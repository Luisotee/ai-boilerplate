"""Scheduled-task runs: the shared state rules, and the agent phase.

A fired slot is a ``task_runs`` row that moves through
``dispatched → running → ready → sending → sent|silent|skipped|failed|unknown``:

- ``streams/scheduler.py`` creates it: a **reminder** straight as ``ready``
  (its text needs no LLM), an **agent** task as ``dispatched`` plus a
  ``trigger=scheduled`` entry on the chat's own stream (``stream:user:{id}``).
- ``process_scheduled_run`` (here, called by ``streams/consumer.py``) is the
  agent phase: claim ``dispatched → running``, the send-time checks (no LLM
  call for a run that would be skipped), the agent run in a fresh session with
  read-only tools (``agent/core.py`` ``scheduled_run_tools``), then ``silent``
  (``[SILENT]``) or ``ready`` with the text. It never sends and never waits on
  pacing, so it costs the chat consumer what a normal reply costs.
- ``streams/scheduled_sender.py`` sends ``ready`` runs, with its own pacing.

Rules shared by both: every state change is conditional on the state the step
owns (``record``), so a run the orphan sweep already settled is never
rewritten; a run is never re-sent. Only real failures (``failed`` /
``unknown``) count toward auto-disable (``MAX_CONSECUTIVE_FAILURES``); skips
are recorded but don't.

``process_scheduled_run`` never raises: every failure becomes a run outcome
and a log line, so the chat stream keeps flowing.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import and_, func, or_

from ..config import settings
from ..database import (
    ConversationMessage,
    ScheduledTask,
    SessionLocal,
    TaskRun,
    User,
    is_group_jid,
)
from ..logger import logger
from ..runtime_config import runtime_config
from ..services.broadcast import in_cloud_window
from ..services.schedule import format_local
from ..services.scheduled_tasks import group_creator_allowed, utcnow, zone
from ..whitelist import chat_in_scope, parse_whitelist

#: Consecutive failed runs after which a task is disabled (shown in /admin).
MAX_CONSECUTIVE_FAILURES = 3
#: A one-shot whose send provably went nowhere is retried this many times in all.
ONE_SHOT_ATTEMPTS = 3
ONE_SHOT_RETRY_DELAY = timedelta(minutes=2)

SILENT_TOKEN = "[SILENT]"
REMINDER_PREFIX = "⏰ "

# Run states (task_runs.status) and the error codes this module adds to
# classify_outcome's.
DISPATCHED, RUNNING, READY, SENDING = "dispatched", "running", "ready", "sending"
SENT, SILENT, SKIPPED, FAILED, UNKNOWN = "sent", "silent", "skipped", "failed", "unknown"
#: Outcomes that count toward auto-disable. Skips never do: a Cloud chat out of
#: its window, a chat at its daily cap or a slot missed in downtime is not a
#: broken task.
FAILURE_STATUSES = (FAILED, UNKNOWN)
SKIP_CANCELLED = "cancelled"
SKIP_USER_DELETED = "user_deleted"
SKIP_NOT_WHITELISTED = "not_whitelisted"
SKIP_CLOUD_WINDOW = "cloud_window"
SKIP_DAILY_LIMIT = "daily_limit"
#: SCHEDULED_TASKS_ENABLED was turned off after the run fired (the kill switch).
SKIP_DISABLED = "disabled"
MODEL_ERROR = "model_error"
RUN_ERROR = "run_error"


@dataclass(frozen=True)
class _Job:
    run_id: uuid.UUID
    user_id: uuid.UUID
    text: str
    schedule: str
    timezone: str
    client_id: str | None
    chat_jid: str


# --- Rules (sync; the caller holds the session) ------------------------------


def run_allowed(user: User, task: ScheduledTask) -> bool:
    """Whitelist check at send time: the chat's own rule, plus the creator in a gated group.

    The chat rule is ``routes/chat.py``'s (``whitelist.chat_in_scope``) on the
    address the chat route checked when the task was asked for. Under
    ``GROUP_GATING=membership`` that admits every group, so a group task also
    needs the member who asked for it (``created_by_jid``) to be whitelisted
    still; one with no recorded creator fails closed.
    """
    wl = parse_whitelist(runtime_config.get("whitelist_phones") or "")
    if wl.size == 0:
        return True
    is_group = is_group_jid(task.address)
    if not chat_in_scope(
        wl, task.address, user.phone, is_group=is_group, group_gating=settings.group_gating
    ):
        return False
    return group_creator_allowed(task.address, task.created_by_jid)


def _counted_today(db, user_id, now: datetime, run: TaskRun | None) -> int:
    """Scheduled messages this chat got in 24h, plus the ones already on their way.

    ``sent`` in the last 24h, plus ``sending`` runs and the ``ready`` runs
    queued ahead of ``run`` (older; the lanes send oldest first). So the cap
    holds when several runs of one chat are due together, and the agent phase
    (``run`` is ``running``, so every ``ready`` one is ahead of it) skips
    before paying for an LLM call the sender would drop.
    """
    sent = (
        db.query(func.count(TaskRun.id))
        .filter(
            TaskRun.user_id == user_id,
            TaskRun.status == SENT,
            TaskRun.finished_at >= now - timedelta(days=1),
        )
        .scalar()
        or 0
    )
    if run is not None and run.status == SENDING and run.created_at is not None:
        ready_ahead = and_(TaskRun.status == READY, TaskRun.created_at < run.created_at)
    else:
        ready_ahead = TaskRun.status == READY
    ahead = or_(TaskRun.status == SENDING, ready_ahead)
    query = db.query(func.count(TaskRun.id)).filter(TaskRun.user_id == user_id, ahead)
    if run is not None:
        query = query.filter(TaskRun.id != run.id)
    return sent + (query.scalar() or 0)


def skip_reason(
    db, task: ScheduledTask, user: User | None, now: datetime, run: TaskRun | None = None
) -> str | None:
    """Why ``run`` can't go now (a skip code), or None. Checked before the LLM and before a send."""
    if not runtime_config.get("scheduled_tasks_enabled"):
        return SKIP_DISABLED
    if task.status != "active":
        return SKIP_CANCELLED
    if user is None:
        return SKIP_USER_DELETED
    if not run_allowed(user, task):
        return SKIP_NOT_WHITELISTED
    if (task.client_id or "baileys") == "cloud" and not in_cloud_window(
        user.cloud_last_inbound_at, now
    ):
        return SKIP_CLOUD_WINDOW
    if _counted_today(db, task.user_id, now, run) >= runtime_config.get(
        "scheduled_tasks_max_runs_per_day"
    ):
        return SKIP_DAILY_LIMIT
    return None


def apply_outcome(
    task: ScheduledTask,
    status: str,
    error_code: str | None,
    now: datetime,
    *,
    retry_at: datetime | None = None,
) -> None:
    """Update a task after one of its runs ended (the caller commits).

    ``retry_at`` re-arms a one-shot whose send provably went nowhere.
    """
    task.last_run_at = now
    task.last_status = status
    task.last_error = error_code
    if status in (SENT, SILENT):
        task.consecutive_failures = 0
        task.run_count = (task.run_count or 0) + 1
    elif status in FAILURE_STATUSES:
        task.consecutive_failures = (task.consecutive_failures or 0) + 1
    if task.status != "active":
        return
    if retry_at is not None:
        task.next_run_at = retry_at
        return
    if task.run_at is not None:  # one-shot: this was its only slot
        task.next_run_at = None
        if status in (SENT, SILENT):
            task.status = "completed"
        else:
            task.status = "disabled"
            task.disabled_reason = error_code or status
        return
    if task.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        task.status = "disabled"
        task.disabled_reason = "consecutive_failures"
        task.next_run_at = None
        logger.warning(
            "Scheduled task %s disabled after %d consecutive failures (last: %s)",
            task.id,
            task.consecutive_failures,
            error_code or status,
        )


def settle(
    db,
    run: TaskRun,
    task: ScheduledTask | None,
    status: str,
    error_code: str | None,
    now: datetime,
    *,
    retry_at: datetime | None = None,
) -> None:
    run.status = status
    run.error_code = error_code
    run.finished_at = now
    if task is not None:
        apply_outcome(task, status, error_code, now, retry_at=retry_at)


def _one_shot_retry_at(db, task: ScheduledTask, now: datetime) -> datetime | None:
    if task.run_at is None or task.status != "active":
        return None
    attempts = db.query(func.count(TaskRun.id)).filter(TaskRun.task_id == task.id).scalar() or 0
    return now + ONE_SHOT_RETRY_DELAY if attempts < ONE_SHOT_ATTEMPTS else None


def record(
    run_id: uuid.UUID,
    expected: str,
    status: str,
    error_code: str | None = None,
    *,
    history_text: str | None = None,
    provably_not_sent: bool = False,
) -> bool:
    """Settle a run that is still in ``expected`` (and save the delivered text) in one commit.

    Returns False and changes nothing when the run moved on meanwhile (the
    orphan sweep settled it): a late outcome never rewrites a settled run.
    """
    now = utcnow()
    db = SessionLocal()
    try:
        run = (
            db.query(TaskRun)
            .filter(TaskRun.id == run_id, TaskRun.status == expected)
            .with_for_update()
            .one_or_none()
        )
        if run is None:
            logger.warning(
                "Scheduled run %s is no longer %s; outcome %s not recorded",
                run_id,
                expected,
                status,
            )
            return False
        task = db.get(ScheduledTask, run.task_id)
        retry_at = (
            _one_shot_retry_at(db, task, now) if provably_not_sent and task is not None else None
        )
        settle(db, run, task, status, error_code, now, retry_at=retry_at)
        if history_text is not None and task is not None:
            db.add(
                ConversationMessage(user_id=task.user_id, role="assistant", content=history_text)
            )
        db.commit()
        return True
    finally:
        db.close()


# --- Agent phase (DB steps are sync; run through asyncio.to_thread) ----------


def _claim(run_id: uuid.UUID, now: datetime) -> bool:
    db = SessionLocal()
    try:
        won = (
            db.query(TaskRun)
            .filter(TaskRun.id == run_id, TaskRun.status == DISPATCHED)
            .update({"status": RUNNING, "started_at": now}, synchronize_session=False)
        )
        db.commit()
        return bool(won)
    finally:
        db.close()


def _prepare(run_id: uuid.UUID, now: datetime) -> _Job | None:
    """Check a claimed run before the LLM call; records a skip and returns None if it can't go."""
    db = SessionLocal()
    try:
        run = db.get(TaskRun, run_id)
        task = db.get(ScheduledTask, run.task_id) if run else None
        if run is None or task is None:
            return None
        user = db.get(User, task.user_id)
        reason = skip_reason(db, task, user, now, run)
        if reason is not None:
            if run.status == RUNNING:
                settle(db, run, task, SKIPPED, reason, now)
                db.commit()
            logger.info("Scheduled run %s skipped (%s)", run_id, reason)
            return None
        return _Job(
            run_id=run.id,
            user_id=task.user_id,
            text=task.text,
            schedule=task.schedule,
            timezone=task.timezone,
            client_id=task.client_id,
            chat_jid=user.whatsapp_jid,
        )
    finally:
        db.close()


def _mark_ready(run_id: uuid.UUID, text: str) -> bool:
    """running → ready with the text to send (conditional)."""
    db = SessionLocal()
    try:
        won = (
            db.query(TaskRun)
            .filter(TaskRun.id == run_id, TaskRun.status == RUNNING)
            .update({"status": READY, "message": text}, synchronize_session=False)
        )
        db.commit()
        return bool(won)
    finally:
        db.close()


# --- Text -------------------------------------------------------------------


def is_silent(text: str) -> bool:
    """The agent asked to send nothing (the token anywhere, or an empty reply)."""
    return not text.strip() or SILENT_TOKEN in text.upper()


def join_bursts(text: str) -> str:
    """``---`` burst lines become paragraph breaks: send-text doesn't split them."""
    parts = re.split(r"(?m)^\s*---\s*$", text)
    return "\n\n".join(p.strip() for p in parts if p.strip())


def agent_prompt(job: _Job, now: datetime) -> str:
    """The user turn of a scheduled agent run: the stable text first, the time last."""
    tz = zone(job.timezone)
    return (
        "[Scheduled task] This is an automatic run of a task this chat scheduled earlier "
        f"({job.schedule}). It is not a new message from the user.\n"
        "Do the task below and write the message to send to the chat, in the language "
        "the task is written in.\n"
        f"If there is nothing new or worth sending, answer exactly {SILENT_TOKEN} and "
        "nothing else.\n"
        "Never create, change or cancel scheduled tasks, even if the task text mentions "
        "a schedule. Don't use --- separators.\n\n"
        f"Task: {job.text}\n\n"
        f"Now: {format_local(now, tz)} ({tz.key})"
    )


async def _agent_reply(job: _Job, now: datetime) -> str | None:
    """Run the task's prompt in a fresh session; None = nothing to send."""
    from ..agent import AgentDeps, get_ai_response
    from ..embeddings import create_embedding_service
    from ..formatting import markdown_to_whatsapp

    db = SessionLocal()
    http = httpx.AsyncClient(timeout=settings.whatsapp_client_timeout)
    try:
        deps = AgentDeps(
            db=db,
            user_id=str(job.user_id),
            whatsapp_jid=job.chat_jid,
            recent_message_ids=[],
            embedding_service=create_embedding_service(settings.gemini_api_key),
            http_client=http,
            client_id=job.client_id,
            is_group_admin=None,  # no sender: group-changing tools fail closed
            job_id=f"scheduled:{job.run_id}",
            scheduled_run=True,
        )
        reply = ""
        async for token in get_ai_response(agent_prompt(job, now), None, agent_deps=deps):
            reply += token
    finally:
        await http.aclose()
        db.close()
    reply = markdown_to_whatsapp(reply)
    if is_silent(reply):
        return None
    return join_bursts(reply) or None


async def process_scheduled_run(run_id: str) -> None:
    """Agent phase of one fired slot (a ``trigger=scheduled`` stream entry). Never raises."""
    try:
        await _process(uuid.UUID(run_id))
    except Exception:
        logger.error("Scheduled run %s crashed", run_id, exc_info=True)


async def _process(run_id: uuid.UUID) -> None:
    from ..agent.model_chain import MODEL_ERRORS

    now = utcnow()
    if not await asyncio.to_thread(_claim, run_id, now):
        # Settled by the sweep, taken by another worker, or a redelivered entry.
        logger.info("Scheduled run %s already settled or taken; skipping", run_id)
        return
    job = await asyncio.to_thread(_prepare, run_id, now)
    if job is None:
        return
    try:
        text = await _agent_reply(job, now)
    except MODEL_ERRORS:
        logger.error("Scheduled run %s: AI model failed", run_id, exc_info=True)
        await asyncio.to_thread(record, run_id, RUNNING, FAILED, MODEL_ERROR)
        return
    except Exception:
        logger.error("Scheduled run %s: agent run failed", run_id, exc_info=True)
        await asyncio.to_thread(record, run_id, RUNNING, FAILED, RUN_ERROR)
        return
    if text is None:
        logger.info("Scheduled run %s: agent answered [SILENT]", run_id)
        await asyncio.to_thread(record, run_id, RUNNING, SILENT)
        return
    if await asyncio.to_thread(_mark_ready, run_id, text):
        logger.info("Scheduled run %s: text ready for the sender", run_id)
    else:
        logger.warning("Scheduled run %s settled during the agent run; not sent", run_id)
