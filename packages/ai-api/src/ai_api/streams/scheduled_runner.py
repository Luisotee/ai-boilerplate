"""Runs one fired slot of a scheduled task: decide the text, deliver it, record it.

``streams/scheduler.py`` fires a slot by adding a ``trigger=scheduled`` job to
the chat's own stream (``stream:user:{id}``), so it is serialised with that
chat's messages; ``streams/consumer.py`` hands it to
``process_scheduled_run``. Rules:

- **Claim before anything else** (``queued|dispatched`` → ``running``, a
  conditional UPDATE committed first). A run the orphan sweep already settled,
  or another worker took, is left alone. A run is never re-sent: ``running``
  left behind by a crash is swept to ``unknown``.
- **Re-checked at send time** (like broadcasts): the task is still active, the
  chat row exists, the chat is whitelisted, a Cloud chat is inside the 23h
  window (no templates), and the chat is under its daily cap.
- **Text**: a ``reminder`` sends its stored text (no LLM). An ``agent`` task
  runs the prompt in a fresh session (no history) with read-only tools
  (``agent/core.py`` ``scheduled_run_tools``), and a reply containing
  ``[SILENT]`` sends nothing.
- **Delivery** reuses the broadcast sender's ``_deliver`` (typing first on
  Baileys) and ``classify_outcome``; Baileys sends from this process are spaced
  a few seconds apart so many tasks due at 09:00 don't go out in one burst.
- **Outcome**: the delivered text is saved to the chat's history in the same
  commit as ``sent``. Failures count toward auto-disable
  (``MAX_CONSECUTIVE_FAILURES``); a one-shot that provably sent nothing is
  re-armed a couple of minutes later (``ONE_SHOT_ATTEMPTS`` in all).

Never raises: every failure becomes a run outcome and a log line, so the chat
stream keeps flowing.
"""

from __future__ import annotations

import asyncio
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func

from ..config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ..database import (
    ConversationMessage,
    ScheduledTask,
    SessionLocal,
    TaskRun,
    User,
    is_group_jid,
    is_telegram_jid,
)
from ..logger import logger
from ..runtime_config import runtime_config
from ..services.broadcast import in_cloud_window
from ..services.schedule import format_local
from ..services.scheduled_tasks import utcnow, zone
from ..whatsapp import create_whatsapp_client
from ..whitelist import is_whitelisted, parse_whitelist
from .broadcast_consumer import (
    FINAL,
    RELEASE,
    RETRY,
    TELEGRAM_SEND_TIMEOUT_SECONDS,
    _deliver,
    classify_outcome,
)

#: Consecutive failed runs after which a task is disabled (shown in /admin).
MAX_CONSECUTIVE_FAILURES = 3
#: A one-shot whose send provably went nowhere is retried this many times in all.
ONE_SHOT_ATTEMPTS = 3
ONE_SHOT_RETRY_DELAY = timedelta(minutes=2)
#: Gap between two scheduled Baileys sends from this process.
BAILEYS_GAP_SECONDS = (3.0, 8.0)
#: Attempts at recording a message that WAS sent.
SENT_RECORD_ATTEMPTS = 3

SILENT_TOKEN = "[SILENT]"
REMINDER_PREFIX = "⏰ "

# Run outcomes (task_runs.status) and the error codes this module adds to
# classify_outcome's.
SENT, SILENT, SKIPPED, FAILED, UNKNOWN = "sent", "silent", "skipped", "failed", "unknown"
SKIP_CANCELLED = "cancelled"
SKIP_USER_DELETED = "user_deleted"
SKIP_NOT_WHITELISTED = "not_whitelisted"
SKIP_CLOUD_WINDOW = "cloud_window"
SKIP_DAILY_LIMIT = "daily_limit"
MODEL_ERROR = "model_error"
RUN_ERROR = "run_error"

_baileys_lock = asyncio.Lock()
_baileys_last_send: float | None = None


@dataclass(frozen=True)
class _Job:
    run_id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    text: str
    schedule: str
    timezone: str
    platform: str
    client_id: str | None
    address: str
    chat_jid: str


# --- DB steps (sync; run through asyncio.to_thread) -------------------------


def _chat_allowed(user: User, address: str) -> bool:
    """Chat whitelist semantics (``routes/chat.py`` ``_is_whitelisted``) for the address.

    The task was asked for in this chat, so the rule is the chat's own: under
    ``GROUP_GATING=membership`` a group is in scope through its members.
    """
    wl = parse_whitelist(runtime_config.get("whitelist_phones") or "")
    if wl.size == 0:
        return True
    if is_group_jid(address):
        return settings.group_gating == "membership" or is_whitelisted(wl, address)
    if is_telegram_jid(address):
        return is_whitelisted(wl, address)
    if is_whitelisted(wl, address, user.phone):
        return True
    return bool(user.whatsapp_lid) and is_whitelisted(wl, user.whatsapp_lid, user.phone)


def _sent_today(db, user_id, now: datetime) -> int:
    return (
        db.query(func.count(TaskRun.id))
        .filter(
            TaskRun.user_id == user_id,
            TaskRun.status == SENT,
            TaskRun.finished_at >= now - timedelta(days=1),
        )
        .scalar()
        or 0
    )


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
    elif not (status == SKIPPED and error_code == SKIP_CANCELLED):
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


def _claim(run_id: uuid.UUID, now: datetime) -> bool:
    db = SessionLocal()
    try:
        won = (
            db.query(TaskRun)
            .filter(TaskRun.id == run_id, TaskRun.status.in_(("queued", "dispatched")))
            .update({"status": "running", "started_at": now}, synchronize_session=False)
        )
        db.commit()
        return bool(won)
    finally:
        db.close()


def _prepare(run_id: uuid.UUID, now: datetime) -> _Job | None:
    """Re-check the run right before sending; records a skip and returns None if it can't go."""
    db = SessionLocal()
    try:
        run = db.get(TaskRun, run_id)
        task = db.get(ScheduledTask, run.task_id) if run else None
        if run is None or task is None:
            return None
        user = db.get(User, task.user_id)
        platform = task.client_id or "baileys"
        reason = None
        if task.status != "active":
            reason = SKIP_CANCELLED
        elif user is None:
            reason = SKIP_USER_DELETED
        elif not _chat_allowed(user, task.address):
            reason = SKIP_NOT_WHITELISTED
        elif platform == "cloud" and not in_cloud_window(user.cloud_last_inbound_at, now):
            reason = SKIP_CLOUD_WINDOW
        elif _sent_today(db, task.user_id, now) >= runtime_config.get(
            "scheduled_tasks_max_runs_per_day"
        ):
            reason = SKIP_DAILY_LIMIT
        if reason is not None:
            _settle(db, run, task, SKIPPED, reason, now)
            db.commit()
            logger.info("Scheduled run %s skipped (%s)", run_id, reason)
            return None
        return _Job(
            run_id=run.id,
            task_id=task.id,
            user_id=task.user_id,
            kind=task.kind,
            text=task.text,
            schedule=task.schedule,
            timezone=task.timezone,
            platform=platform,
            client_id=task.client_id,
            address=task.address,
            chat_jid=user.whatsapp_jid,
        )
    finally:
        db.close()


def _settle(
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


def _record(
    run_id: uuid.UUID,
    status: str,
    error_code: str | None = None,
    *,
    history_text: str | None = None,
    provably_not_sent: bool = False,
) -> None:
    """Store a run's outcome (and the delivered text in the chat's history) in one commit."""
    now = utcnow()
    db = SessionLocal()
    try:
        run = db.get(TaskRun, run_id)
        if run is None:
            return
        task = db.get(ScheduledTask, run.task_id)
        retry_at = (
            _one_shot_retry_at(db, task, now) if provably_not_sent and task is not None else None
        )
        _settle(db, run, task, status, error_code, now, retry_at=retry_at)
        if history_text is not None and task is not None:
            db.add(
                ConversationMessage(user_id=task.user_id, role="assistant", content=history_text)
            )
        db.commit()
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


# --- Delivery ---------------------------------------------------------------


async def _baileys_gap() -> None:
    """Space scheduled Baileys sends from this process a few seconds apart."""
    global _baileys_last_send
    loop = asyncio.get_running_loop()
    if _baileys_last_send is not None:
        wait = _baileys_last_send + random.uniform(*BAILEYS_GAP_SECONDS) - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
    _baileys_last_send = loop.time()


async def _send(job: _Job, text: str) -> None:
    timeout = (
        TELEGRAM_SEND_TIMEOUT_SECONDS
        if job.platform == "telegram"
        else settings.whatsapp_client_timeout
    )
    async with httpx.AsyncClient(timeout=timeout) as http:
        client = create_whatsapp_client(
            http_client=http,
            base_url=get_whatsapp_client_url(job.client_id),
            api_key=get_whatsapp_api_key(job.client_id),
        )
        if job.platform == "baileys":
            async with _baileys_lock:
                await _baileys_gap()
                await _deliver(client, job.platform, job.address, text)
        else:
            await _deliver(client, job.platform, job.address, text)


async def _record_sent(job: _Job, text: str) -> None:
    for attempt in range(1, SENT_RECORD_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(_record, job.run_id, SENT, history_text=text)
            return
        except Exception:
            if attempt == SENT_RECORD_ATTEMPTS:
                # Left 'running': the sweep records it as 'unknown', never re-sent.
                logger.error(
                    "Scheduled run %s was sent but could not be recorded", job.run_id, exc_info=True
                )
                return
            await asyncio.sleep(0.5 * 2 ** (attempt - 1))


async def process_scheduled_run(run_id: str) -> None:
    """Run one fired slot. Never raises."""
    try:
        await _process(uuid.UUID(run_id))
    except Exception:
        logger.error("Scheduled run %s crashed", run_id, exc_info=True)


async def _process(run_id: uuid.UUID) -> None:
    from ..agent.model_chain import MODEL_ERRORS

    now = utcnow()
    if not await asyncio.to_thread(_claim, run_id, now):
        logger.info("Scheduled run %s already settled or taken; skipping", run_id)
        return
    job = await asyncio.to_thread(_prepare, run_id, now)
    if job is None:
        return

    if job.kind == "agent":
        try:
            text = await _agent_reply(job, now)
        except MODEL_ERRORS:
            logger.error("Scheduled run %s: AI model failed", run_id, exc_info=True)
            await asyncio.to_thread(_record, run_id, FAILED, MODEL_ERROR)
            return
        except Exception:
            logger.error("Scheduled run %s: agent run failed", run_id, exc_info=True)
            await asyncio.to_thread(_record, run_id, FAILED, RUN_ERROR)
            return
        if text is None:
            logger.info("Scheduled run %s: agent answered [SILENT]", run_id)
            await asyncio.to_thread(_record, run_id, SILENT)
            return
    else:
        text = REMINDER_PREFIX + job.text

    try:
        await _send(job, text)
    except Exception as e:
        kind, code = classify_outcome(e)
        logger.warning("Scheduled run %s: send failed (%s, %s)", run_id, kind, code)
        status = FAILED if kind in (FINAL, RELEASE, RETRY) else UNKNOWN
        await asyncio.to_thread(
            _record, run_id, status, code, provably_not_sent=kind in (RELEASE, RETRY)
        )
        return
    await _record_sent(job, text)
    logger.info("Scheduled run %s sent to %s…", run_id, job.address[:8])
