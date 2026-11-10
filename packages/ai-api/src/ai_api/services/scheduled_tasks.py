"""Scheduled tasks: create, list and cancel (shared by the agent tool, /admin and /clean).

The scheduling math is in ``services/schedule.py``; firing is in
``streams/scheduler.py`` and delivery in ``streams/scheduled_runner.py``.
Every function here takes the caller's session and leaves committing to it,
except ``create_task``, which commits the new row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import get_args
from zoneinfo import ZoneInfo

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from ..broadcast_pacing import parse_timezone
from ..config import settings
from ..database import ScheduledTask, is_group_jid
from ..formatting import markdown_to_whatsapp
from ..logger import logger
from ..runtime_config import runtime_config
from ..schemas import ScheduledTaskKind, ScheduledTaskStatus
from ..surfaces import delivers_through_chat_client
from ..whitelist import is_whitelisted, parse_whitelist
from .schedule import (
    ScheduleError,
    format_local,
    max_per_day,
    min_gap,
    next_fire,
    parse_schedule,
    upcoming,
)

KINDS: tuple[str, ...] = get_args(ScheduledTaskKind)
STATUSES: tuple[str, ...] = get_args(ScheduledTaskStatus)

MAX_REMINDER_CHARS = 1000
MAX_AGENT_PROMPT_CHARS = 2000
#: An agent task runs the LLM on every fire: at most this many fires per 24h
#: (nanoclaw's MAX_DAILY_FIRES for ungated recurrences).
AGENT_MAX_DAILY_FIRES = 4
#: How far ahead the frequency checks look at a recurring schedule.
_LOOKAHEAD = timedelta(days=8)
#: Shortest id prefix accepted when cancelling.
MIN_ID_PREFIX = 4


class TaskError(ValueError):
    """The request can't be honoured; the message is safe to show the user."""


def utcnow() -> datetime:
    """Naive UTC, matching the DB's ``datetime.utcnow`` columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def bot_timezone() -> ZoneInfo:
    """The bot's time zone: ``bot_timezone``, else ``broadcast_timezone``, else UTC."""
    name = runtime_config.get("bot_timezone") or runtime_config.get("broadcast_timezone") or "UTC"
    try:
        return parse_timezone(name)
    except ValueError:
        # config.py and PATCH /admin/settings reject bad values; this only
        # guards against one corrupted in the DB by hand.
        logger.error("Scheduled tasks: invalid time zone %r; using UTC", name)
        return ZoneInfo("UTC")


def zone(name: str | None) -> ZoneInfo:
    """A stored zone name, or UTC if it no longer resolves."""
    try:
        return parse_timezone(name or "UTC")
    except ValueError:
        return ZoneInfo("UTC")


def task_timezone(task: ScheduledTask) -> ZoneInfo:
    return zone(task.timezone)


def short_id(task: ScheduledTask) -> str:
    return str(task.id).replace("-", "")[:8]


def describe_task(task: ScheduledTask, tz: ZoneInfo | None = None) -> str:
    """One line for the user: id, kind, schedule, next run and last outcome."""
    tz = tz or task_timezone(task)
    text = task.text if len(task.text) <= 80 else task.text[:77] + "..."
    parts = [f"#{short_id(task)}", task.kind, task.schedule]
    if task.status == "active" and task.next_run_at:
        parts.append(f"next {format_local(task.next_run_at, tz)}")
    elif task.status != "active":
        parts.append(task.status)
    if task.last_status:
        parts.append(f"last {task.last_status}")
    return " | ".join(parts) + f': "{text}"'


def group_creator_allowed(address: str, created_by_jid: str | None) -> bool:
    """Under ``GROUP_GATING=membership``, a group task needs its creator whitelisted.

    The chat rule admits every group in that mode (the client gates by
    sender), so this is what stops a group task once the member who asked for
    it leaves the whitelist. Checked at creation and before every run; no
    recorded creator fails closed.

    Matched on ``created_by_jid`` (the sender's phone JID on Baileys,
    ``tg:<id>`` on Telegram) only: the AI API never receives the raw ``@lid``
    participant, so a creator whitelisted only by a raw ``…@lid`` entry is
    refused at creation (with a reason) rather than skipped silently later.
    """
    if not (is_group_jid(address) and settings.group_gating == "membership"):
        return True
    wl = parse_whitelist(runtime_config.get("whitelist_phones") or "")
    if wl.size == 0:
        return True
    return bool(created_by_jid) and is_whitelisted(wl, created_by_jid)


def active_count(db: Session, user_id) -> int:
    return (
        db.query(ScheduledTask)
        .filter(ScheduledTask.user_id == user_id, ScheduledTask.status == "active")
        .count()
    )


def create_task(
    db: Session,
    *,
    user_id,
    address: str,
    client_id: str | None,
    kind: str,
    text: str,
    schedule_text: str,
    created_by_name: str | None = None,
    created_by_jid: str | None = None,
    now: datetime | None = None,
) -> ScheduledTask:
    """Validate, create and commit a task. Raises ``TaskError`` with a user-facing reason."""
    now = now or utcnow()
    if kind not in KINDS:
        raise TaskError("kind must be 'reminder' or 'agent'.")
    text = (text or "").strip()
    limit = MAX_REMINDER_CHARS if kind == "reminder" else MAX_AGENT_PROMPT_CHARS
    if not text:
        raise TaskError("Say what to send (the reminder text, or what the task should do).")
    if len(text) > limit:
        raise TaskError(f"That text is too long (max {limit} characters).")
    if kind == "reminder":
        # The text is model output and is sent as is: same Markdown rule as replies.
        text = markdown_to_whatsapp(text).strip() or text

    if not delivers_through_chat_client(client_id, address):
        # Defense in depth (the agent tool is hidden there): nothing could deliver it.
        raise TaskError("Scheduled messages can only be sent on WhatsApp and Telegram.")
    if not group_creator_allowed(address, created_by_jid):
        raise TaskError(
            "I can't confirm you're on this bot's whitelist by phone number, so a scheduled "
            "message in this group would never be sent. Ask the bot's operator to add your "
            "phone number to the whitelist."
        )

    tz = bot_timezone()
    try:
        schedule = parse_schedule(schedule_text, tz, now)
    except ScheduleError as e:
        raise TaskError(str(e)) from e

    max_tasks = runtime_config.get("scheduled_tasks_max_per_chat")
    if active_count(db, user_id) >= max_tasks:
        raise TaskError(
            f"This chat already has {max_tasks} active scheduled tasks, the most allowed. "
            "Cancel one first."
        )

    if schedule.recurring:
        min_minutes = runtime_config.get("scheduled_tasks_min_interval_minutes")
        next_run_at = next_fire(
            tz=tz,
            after=now,
            cron_expr=schedule.cron_expr,
            interval_seconds=schedule.interval_seconds,
        )
        if next_run_at is None:
            raise TaskError("That schedule never fires.")
        # Measured from the first slot, not from now: '* * 1 * *' has no slot in
        # the next 8 days but fires every minute on the 1st.
        slots = upcoming(schedule, tz, next_run_at - timedelta(seconds=1), _LOOKAHEAD)
        gap = (
            timedelta(seconds=schedule.interval_seconds)
            if schedule.interval_seconds
            else min_gap(slots)
        )
        if gap is not None and gap < timedelta(minutes=min_minutes):
            raise TaskError(
                f"That repeats too often: the shortest allowed gap is {min_minutes} minutes."
            )
        if kind == "agent" and max_per_day(slots) > AGENT_MAX_DAILY_FIRES:
            raise TaskError(
                f"A task that runs the assistant may fire at most {AGENT_MAX_DAILY_FIRES} "
                "times a day."
            )
    else:
        next_run_at = schedule.run_at

    task = ScheduledTask(
        user_id=user_id,
        client_id=client_id,
        address=address,
        created_by_name=(created_by_name or None),
        created_by_jid=(created_by_jid or None),
        kind=kind,
        text=text,
        schedule=schedule.description,
        run_at=schedule.run_at,
        cron_expr=schedule.cron_expr,
        interval_seconds=schedule.interval_seconds,
        timezone=tz.key,
        status="active",
        next_run_at=next_run_at,
        consecutive_failures=0,
        run_count=0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info(
        "Scheduled task %s created (%s, %s, next %s)", task.id, kind, task.schedule, next_run_at
    )
    return task


def list_tasks(db: Session, user_id) -> list[ScheduledTask]:
    """This chat's active tasks, soonest first."""
    return (
        db.query(ScheduledTask)
        .filter(ScheduledTask.user_id == user_id, ScheduledTask.status == "active")
        .order_by(ScheduledTask.next_run_at.asc().nulls_last())
        .all()
    )


def find_task(db: Session, user_id, ref: str) -> ScheduledTask:
    """This chat's task whose id starts with ``ref`` (``#`` and dashes ignored).

    Raises ``TaskError`` when none or several match. Scoped to the chat, so a
    guessed id can never touch another chat's task; only active and disabled
    tasks match (a finished one can't make a prefix ambiguous).
    """
    ref = (ref or "").strip().lstrip("#").replace("-", "").lower()
    if len(ref) < MIN_ID_PREFIX or any(c not in "0123456789abcdef" for c in ref):
        raise TaskError("Give the task id shown by the list action (like #ab12cd34).")
    matches = (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.user_id == user_id,
            ScheduledTask.status.in_(("active", "disabled")),
            func.replace(cast(ScheduledTask.id, String), "-", "").like(f"{ref}%"),
        )
        .limit(2)
        .all()
    )
    if not matches:
        raise TaskError(f"No scheduled task #{ref} in this chat.")
    if len(matches) > 1:
        raise TaskError(f"Several tasks start with #{ref}; give more of the id.")
    return matches[0]


def cancel_task(db: Session, user_id, ref: str) -> ScheduledTask:
    """Cancel one of this chat's tasks (not committed). Raises ``TaskError``."""
    task = find_task(db, user_id, ref)
    task.status = "cancelled"
    task.next_run_at = None
    return task


def cancel_chat_tasks(db: Session, user_id) -> int:
    """Cancel every active or disabled task of a chat (``/clean all``); not committed."""
    return (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.user_id == user_id,
            ScheduledTask.status.in_(("active", "disabled")),
        )
        .update({"status": "cancelled", "next_run_at": None}, synchronize_session=False)
    )


def rearm(task: ScheduledTask, now: datetime) -> datetime | None:
    """Next slot for a task being (re)enabled now; None if it can never fire again."""
    return next_fire(
        tz=task_timezone(task),
        after=now,
        run_at=task.run_at,
        cron_expr=task.cron_expr,
        interval_seconds=task.interval_seconds,
    )
