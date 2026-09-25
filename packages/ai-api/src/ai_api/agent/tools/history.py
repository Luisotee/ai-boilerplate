"""
Current-conversation history tool.

Lets the agent pull more of THIS chat's messages chronologically — a flat last-N
count and/or a recent time window — beyond the fixed recent history that is
auto-injected at the start of each run. Works in private chats and in the group
the bot is currently in. For semantic/topic recall use search_conversation_history
instead.
"""

from datetime import UTC, datetime, timedelta

from pydantic_ai import RunContext

from ...database import get_conversation_messages
from ...logger import logger
from ...rag.conversation import format_transcript
from ...runtime_config import runtime_config
from ..core import AgentDeps, agent
from ._db import safe_rollback


@agent.tool
async def get_chat_history(
    ctx: RunContext[AgentDeps],
    limit: int | None = None,
    since_hours: int | None = None,
) -> str:
    """
    Fetch more of THIS conversation's messages, chronologically.

    Use when the recent history already in context isn't enough — to recall older
    messages, an explicit number of them, or everything from a recent time window.

    - `limit`: return the last N messages (e.g. 50).
    - `since_hours`: only messages from the last N hours (e.g. 24 = last day,
      168 = last week). Combine with `limit` to cap a window.
    - Omit both to get the most recent messages (up to a capped amount); prefer
      passing a modest `limit` so the reply stays focused.

    For finding a specific TOPIC use search_conversation_history instead.

    Args:
        ctx: Run context with db and the current conversation's user_id.
        limit: Optional flat cap on how many recent messages to return.
        since_hours: Optional time window — only messages from the last N hours.

    Returns:
        A chronological transcript of this chat, or an explanatory message.
    """
    logger.info(
        f"TOOL CALLED: get_chat_history limit={limit!r} since_hours={since_hours!r} "
        f"jid={ctx.deps.whatsapp_jid}"
    )

    deps = ctx.deps

    try:
        since = None
        if since_hours is not None and since_hours > 0:
            # The timestamp column is naive UTC.
            since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=since_hours)

        # Keyed by the run's own user row: this tool can only ever read the
        # conversation the message came from.
        messages = get_conversation_messages(deps.db, deps.user_id, limit=limit, since=since)
        if not messages:
            if since is not None:
                return f"There are no messages in this conversation from the last {since_hours}h."
            return "I don't have any stored messages for this conversation yet."

        transcript = format_transcript(
            messages,
            assistant_label=runtime_config.get("bot_name"),
            with_timestamps=since is not None,
            default_user_label="User",
        )
        logger.info(f"get_chat_history: returned {len(messages)} messages")
        return transcript

    except Exception:
        # Shared DB session: roll back so later tools don't hit PendingRollbackError.
        safe_rollback(deps.db)
        logger.error("get_chat_history failed", exc_info=True)
        return "I couldn't read the conversation history right now. Please try again shortly."
