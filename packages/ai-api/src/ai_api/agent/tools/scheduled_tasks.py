"""Reminders and recurring messages: one tool with create / list / cancel.

The model passes the user's intent in the schedule grammar of
``services/schedule.py`` and never computes a date itself. Identity comes from
``ctx.deps`` only. A task delivers to the chat it belongs to: in a group, that
group (create/cancel by a confirmed admin, fail closed, ``group_admin_refusal``;
any member may list). From a PRIVATE chat, ``group_name`` schedules into a group
the user shares with the bot (strict match, like ``send_group_message``) where
they are a confirmed admin, asked live of the chat client; the task stores the
requester as ``created_from_user_id`` and the scheduler re-checks membership and
admin role before every run (``services/task_creator.py``).
"""

import uuid
from typing import Literal

import httpx
from pydantic_ai import RunContext, ToolDefinition

from ...config import get_whatsapp_api_key, get_whatsapp_client_url, settings
from ...database import (
    ConversationMessage,
    User,
    get_or_create_user,
    is_group_jid,
)
from ...logger import logger
from ...runtime_config import runtime_config
from ...services.schedule import format_local
from ...services.scheduled_tasks import (
    TaskError,
    cancel_task,
    create_task,
    describe_task,
    find_task,
    list_tasks,
    list_tasks_created_from,
    short_id,
    task_timezone,
)
from ...services.shared_groups import (
    match_shared_group,
    platform_of,
    requester_group_status,
    resolve_shared_groups,
    telegram_identity,
)
from ...surfaces import delivers_through_chat_client
from ...whatsapp import create_whatsapp_client
from ...whatsapp.client import SharedGroup
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal
from ._surface import chat_client_only

CLOUD_WINDOW_NOTE = (
    " Note: on this WhatsApp number I can only message you within 24 hours of your "
    "last message, so a run due later than that is skipped."
)


async def scheduled_tasks_enabled(
    ctx: RunContext[AgentDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """`prepare` hook: hide the tool when scheduling is off, in scheduled runs, and
    on the app surfaces (``/v1``, terminal chat), which have no chat client to
    deliver a task through (``_surface.chat_client_only``)."""
    if (ctx.deps is not None and ctx.deps.scheduled_run) or not runtime_config.get(
        "scheduled_tasks_enabled"
    ):
        return None
    if ctx.deps is None:  # no run context (e.g. schema introspection): as before
        return tool_def
    return await chat_client_only(ctx, tool_def)


def _creator(deps: AgentDeps) -> tuple[str | None, str | None]:
    """(sender_jid, sender_name) of the message being answered, if it is known."""
    if not deps.user_message_id:
        return None, None
    try:
        msg = deps.db.get(ConversationMessage, uuid.UUID(str(deps.user_message_id)))
    except ValueError:
        return None, None
    return (msg.sender_jid, msg.sender_name) if msg is not None else (None, None)


class _Refusal(Exception):
    """A user-facing refusal from the group-target checks."""


def _group_name(db, user_id) -> str:
    row = db.get(User, user_id)
    return (row.name or row.whatsapp_jid) if row is not None else "a group"


def _creator_jid(user: User, platform: str) -> str | None:
    """The requester's sender id as a group message would carry it."""
    if platform == "telegram":
        return telegram_identity(user)
    if user.whatsapp_jid and user.whatsapp_jid.endswith("@s.whatsapp.net"):
        return user.whatsapp_jid
    phone = (user.phone or "").lstrip("+")
    return f"{phone}@s.whatsapp.net" if phone else user.whatsapp_jid


async def _admin_status(deps: AgentDeps, platform: str, user: User, group_jid: str):
    """``(is_member, is_admin)`` asked live of ``platform``'s client (raises on failure)."""
    if deps.whatsapp_client is not None and platform_of(deps.client_id) == platform:
        return await requester_group_status(deps.whatsapp_client, platform, user, group_jid)
    async with httpx.AsyncClient(timeout=settings.whatsapp_client_timeout) as http:
        client = create_whatsapp_client(
            http_client=http,
            base_url=get_whatsapp_client_url(platform),
            api_key=get_whatsapp_api_key(platform),
        )
        return await requester_group_status(client, platform, user, group_jid)


async def _target_group(deps: AgentDeps, user: User, group_name: str) -> SharedGroup:
    """The shared group ``group_name`` names, with the requester a confirmed admin there."""
    platform = platform_of(deps.client_id)
    if platform == "cloud" or not delivers_through_chat_client(deps.client_id, deps.whatsapp_jid):
        raise _Refusal("I can only schedule into a group from WhatsApp or Telegram.")
    groups = await resolve_shared_groups(deps, user)
    if not groups:
        raise _Refusal(
            "I couldn't find any group that you and I are both in, so I can't schedule there."
        )
    match = match_shared_group(groups, group_name, strict=True)
    if match is None:
        names = ", ".join(g.subject for g in groups)
        raise _Refusal(
            f'I\'m not sure which group you mean by "{group_name}". '
            f"The groups we share are: {names}. Which one?"
        )
    if platform == "telegram":
        # The Telegram list is derived from stored messages: ask live.
        is_member, is_admin = await _admin_status(deps, platform, user, match.group_jid)
        if not is_member:
            raise _Refusal(f"You're no longer in \"{match.subject}\", so I can't schedule there.")
    else:
        is_admin = match.is_admin  # the Baileys list was just resolved live
    if is_admin is not True:
        raise _Refusal(
            f'Only an admin of "{match.subject}" can schedule messages there, and I '
            "couldn't confirm you are one."
        )
    return match


def _same_group(deps: AgentDeps, group_name: str) -> bool:
    """Whether ``group_name`` names the group this message came from."""
    here = SharedGroup(deps.whatsapp_jid, _group_name(deps.db, deps.user_id))
    return match_shared_group([here], group_name, strict=True) is not None


@agent.tool(prepare=scheduled_tasks_enabled)
async def manage_scheduled_tasks(
    ctx: RunContext[AgentDeps],
    action: Literal["create", "list", "cancel"],
    schedule: str | None = None,
    text: str | None = None,
    kind: Literal["reminder", "agent"] = "reminder",
    task_id: str | None = None,
    group_name: str | None = None,
) -> str:
    """
    Create, list or cancel reminders and recurring messages, when the user wants
    to be reminded or wants a message at a set time or on a schedule. In a group,
    a group admin schedules there. From a private chat, pass group_name to
    schedule into (or cancel from) a group you share with the bot where the user
    is an admin.

    Never compute dates yourself: pass the user's timing in `schedule`, as one of
    - once: "in 30m", "in 2h", "tomorrow at 9:00", "monday at 9:00",
      "next monday at 9:00", "2026-11-20 09:00"
    - repeating: "every 2h", "every day at 9:00", "every weekday at 8:30",
      "every monday,friday at 9:00", or a cron like "0 9 * * 1"
    Times are the bot's local time. After creating, restate the schedule and
    next run exactly as returned.

    Args:
        action: "list" shows this chat's tasks
        schedule: For create: when
        text: For create: the reminder text, or for kind "agent" the instruction
            to carry out at each run
        kind: "reminder" sends `text` as is; "agent" runs it and sends the result
            (only when something must be looked up or produced at run time)
        task_id: For cancel: the id shown by list, like "ab12cd34"
        group_name: Private chat only: the exact name of the group to schedule into
    """
    logger.info(f"⏰ TOOL CALLED: manage_scheduled_tasks (action={action}, kind={kind})")
    deps = ctx.deps
    in_group = is_group_jid(deps.whatsapp_jid)
    if group_name is not None and not group_name.strip():
        group_name = None
    if in_group and group_name is not None:
        if not _same_group(deps, group_name):
            return (
                "To schedule a message in another group, ask me in a private chat. "
                "Here I can only schedule for this group."
            )
        group_name = None  # names this same group: the usual group path
    try:
        if action == "list":
            tasks = list_tasks(deps.db, deps.user_id)
            lines = [describe_task(t) for t in tasks]
            if not in_group:
                for t in list_tasks_created_from(deps.db, deps.user_id):
                    lines.append(f"[in {_group_name(deps.db, t.user_id)}] {describe_task(t)}")
            if not lines:
                return "There are no active scheduled tasks in this chat."
            return "Active scheduled tasks:\n" + "\n".join(lines)

        if group_admin_refusal(deps, "manage scheduled tasks", ""):
            return (
                "Only a group admin can schedule or cancel messages for this group. "
                "An admin can ask me."
            )

        if action == "cancel":
            return await _cancel(deps, task_id or "", group_name, in_group)

        if group_name is not None:
            return await _create_in_group(deps, group_name, kind, text, schedule)

        created_by_jid, created_by_name = _creator(deps)
        task = create_task(
            deps.db,
            user_id=deps.user_id,
            address=deps.whatsapp_jid,
            client_id=deps.client_id,
            kind=kind,
            text=text or "",
            schedule_text=schedule or "",
            created_by_name=created_by_name,
            created_by_jid=created_by_jid,
        )
        reply = _created_reply(task, "this group" if in_group else "this chat")
        if deps.client_id == "cloud":
            reply += CLOUD_WINDOW_NOTE
        return reply
    except _Refusal as e:
        safe_rollback(deps.db)
        return str(e)
    except TaskError as e:
        safe_rollback(deps.db)
        return str(e)
    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error managing scheduled tasks: {e}", exc_info=True)
        return "Failed to manage scheduled tasks. Please try again."


def _created_reply(task, target: str) -> str:
    tz = task_timezone(task)
    return f"Scheduled #{short_id(task)}: {task.schedule} ({tz.key}), next run " + (
        f"{format_local(task.next_run_at, tz)}. "
        + (
            f'I\'ll send to {target}: "{task.text}".'
            if task.kind == "reminder"
            else f'At each run I\'ll do: "{task.text}" and send the result to {target} '
            "(nothing if there's nothing new)."
        )
    )


def _requester(deps: AgentDeps) -> User:
    user = deps.db.get(User, uuid.UUID(str(deps.user_id)))
    if user is None:
        raise _Refusal("I couldn't identify your account.")
    return user


async def _create_in_group(deps: AgentDeps, group_name: str, kind, text, schedule) -> str:
    """Schedule into a shared group from this private chat (confirmed admin only)."""
    user = _requester(deps)
    match = await _target_group(deps, user, group_name)
    platform = platform_of(deps.client_id)
    group = get_or_create_user(deps.db, match.group_jid, "group", name=match.subject)
    task = create_task(
        deps.db,
        user_id=group.id,
        address=match.group_jid,
        client_id=deps.client_id,
        kind=kind,
        text=text or "",
        schedule_text=schedule or "",
        created_by_name=user.name,
        created_by_jid=_creator_jid(user, platform),
        created_from_user_id=user.id,
    )
    logger.info("Scheduled task %s created into group %s from private", task.id, match.group_jid)
    return _created_reply(task, f'the group "{match.subject}"') + (
        " It runs only while you're still an admin there."
    )


async def _cancel(deps: AgentDeps, ref: str, group_name: str | None, in_group: bool) -> str:
    """Cancel one of this chat's tasks, or (private) one it scheduled into a group."""
    if in_group or group_name is None:
        try:
            task = cancel_task(deps.db, deps.user_id, ref)
            deps.db.commit()
            return f"Cancelled #{short_id(task)} ({task.schedule})."
        except TaskError:
            if in_group:
                raise
            deps.db.rollback()
    # A task this private chat scheduled into a group (user_id never matches NULL).
    task = find_task(deps.db, None, ref, created_from=deps.user_id)
    if group_name is not None and not match_shared_group(
        [SharedGroup(task.address, _group_name(deps.db, task.user_id))], group_name, strict=True
    ):
        raise _Refusal(f'Task #{short_id(task)} isn\'t in the group "{group_name}".')
    user = _requester(deps)
    # Ask the client the task runs on (a linked user may be chatting on the other one).
    platform = task.client_id or "baileys"
    try:
        is_member, is_admin = await _admin_status(deps, platform, user, task.address)
    except Exception:
        logger.warning("Cancel of task %s: admin lookup failed", task.id, exc_info=True)
        raise _Refusal(
            "I couldn't confirm you're still an admin of that group, so I didn't cancel it. "
            "Please try again shortly."
        ) from None
    if not (is_member and is_admin is True):
        raise _Refusal("Only an admin of that group can cancel its scheduled messages.")
    task.status = "cancelled"
    task.next_run_at = None
    deps.db.commit()
    return f"Cancelled #{short_id(task)} in {_group_name(deps.db, task.user_id)} ({task.schedule})."
