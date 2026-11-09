"""Reminders and recurring messages: one tool with create / list / cancel.

The model passes the user's intent in the schedule grammar of
``services/schedule.py`` and never computes a date itself. Identity and target
come from ``ctx.deps`` only: a task always delivers to the chat it was created
in, so a group's task reaches only that group and a private chat's task can
never target a group. In a group only a confirmed admin may create or cancel
(fail closed, ``group_admin_refusal``); any member may list.
"""

import uuid
from typing import Literal

from pydantic_ai import RunContext, ToolDefinition

from ...database import ConversationMessage, is_group_jid
from ...logger import logger
from ...runtime_config import runtime_config
from ...services.schedule import format_local
from ...services.scheduled_tasks import (
    TaskError,
    cancel_task,
    create_task,
    describe_task,
    list_tasks,
    short_id,
    task_timezone,
)
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal

CLOUD_WINDOW_NOTE = (
    " Note: on this WhatsApp number I can only message you within 24 hours of your "
    "last message, so a run due later than that is skipped."
)


async def scheduled_tasks_enabled(
    ctx: RunContext[AgentDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """`prepare` hook: hide the tool when scheduling is off, and in scheduled runs."""
    if (ctx.deps is not None and ctx.deps.scheduled_run) or not runtime_config.get(
        "scheduled_tasks_enabled"
    ):
        return None
    return tool_def


def _creator(deps: AgentDeps) -> tuple[str | None, str | None]:
    """(sender_jid, sender_name) of the message being answered, if it is known."""
    if not deps.user_message_id:
        return None, None
    try:
        msg = deps.db.get(ConversationMessage, uuid.UUID(str(deps.user_message_id)))
    except ValueError:
        return None, None
    return (msg.sender_jid, msg.sender_name) if msg is not None else (None, None)


@agent.tool(prepare=scheduled_tasks_enabled)
async def manage_scheduled_tasks(
    ctx: RunContext[AgentDeps],
    action: Literal["create", "list", "cancel"],
    schedule: str | None = None,
    text: str | None = None,
    kind: Literal["reminder", "agent"] = "reminder",
    task_id: str | None = None,
) -> str:
    """
    Create, list or cancel reminders and recurring messages in this chat.

    Never compute dates yourself: pass the user's timing in `schedule`, as one of
    - once: "in 30m", "in 2h", "in 1d", "at 18:30", "tomorrow at 9:00",
      "monday at 9:00", "next monday at 9:00", "2026-11-20 09:00"
    - repeating: "every 2h", "every day at 9:00", "every weekday at 8:30",
      "every monday,friday at 9:00", or a 5-field cron like "0 9 * * 1"
    Times are the bot's local time. After creating, tell the user the schedule
    and next run exactly as returned.

    Args:
        action: "create", "list" (this chat's tasks) or "cancel".
        schedule: For create: when, in the grammar above.
        text: For create: the reminder text to send, or for kind "agent" the
            instruction to carry out at each run (e.g. "check the news on X and
            tell me only if something changed").
        kind: "reminder" sends `text` as is; "agent" runs `text` as a task and
            sends the result (use only when the user wants something looked up
            or produced at run time).
        task_id: For cancel: the id shown by list (like "ab12cd34").
    """
    logger.info(f"⏰ TOOL CALLED: manage_scheduled_tasks (action={action}, kind={kind})")
    deps = ctx.deps
    try:
        if action == "list":
            tasks = list_tasks(deps.db, deps.user_id)
            if not tasks:
                return "There are no active scheduled tasks in this chat."
            return "Active scheduled tasks in this chat:\n" + "\n".join(
                describe_task(t) for t in tasks
            )

        if group_admin_refusal(deps, "manage scheduled tasks", ""):
            return (
                "Only a group admin can schedule or cancel messages for this group. "
                "An admin can ask me."
            )

        if action == "cancel":
            task = cancel_task(deps.db, deps.user_id, task_id or "")
            deps.db.commit()
            return f"Cancelled #{short_id(task)} ({task.schedule})."

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
        tz = task_timezone(task)
        target = "this group" if is_group_jid(deps.whatsapp_jid) else "this chat"
        reply = (
            f"Scheduled #{short_id(task)}: {task.schedule} ({tz.key}), next run "
            f"{format_local(task.next_run_at, tz)}. "
            + (
                f'I\'ll send to {target}: "{task.text}".'
                if task.kind == "reminder"
                else f'At each run I\'ll do: "{task.text}" and send the result to {target} '
                "(nothing if there's nothing new)."
            )
        )
        if deps.client_id == "cloud":
            reply += CLOUD_WINDOW_NOTE
        return reply
    except TaskError as e:
        safe_rollback(deps.db)
        return str(e)
    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error managing scheduled tasks: {e}", exc_info=True)
        return "Failed to manage scheduled tasks. Please try again."
