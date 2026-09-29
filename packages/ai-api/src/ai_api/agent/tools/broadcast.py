"""Opt in/out of operator broadcasts by asking the bot ("stop sending me updates")."""

from pydantic_ai import RunContext

from ...commands import set_broadcast_opt_out
from ...database import is_group_jid
from ...logger import logger
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal


@agent.tool
async def set_broadcast_subscription(ctx: RunContext[AgentDeps], subscribed: bool) -> str:
    """
    Turn update announcements (broadcasts) on or off for this chat.

    The bot's operators occasionally send an announcement (new features, news)
    to everyone. Use this when the user asks to stop or resume those messages
    (e.g., "stop sending me updates", "unsubscribe from announcements", "I don't
    want these news messages", "send me the updates again").

    In a group chat this changes the setting for the whole group, and only a
    group admin may do that.

    Args:
        ctx: Run context with database and user info
        subscribed: False to stop receiving announcements, True to receive them again

    Returns:
        Confirmation message describing the result
    """
    logger.info(f"📣 TOOL CALLED: set_broadcast_subscription (subscribed={subscribed})")
    deps = ctx.deps
    try:
        refusal = group_admin_refusal(
            deps, "turn announcements on or off for this group", "/broadcast off"
        )
        if refusal:
            return refusal
        is_group = is_group_jid(deps.whatsapp_jid)

        # Identity comes from the run's own user row (deps.user_id), never from
        # arguments; None means that row doesn't exist.
        previous = set_broadcast_opt_out(deps.db, deps.user_id, opt_out=not subscribed)
        if previous is None:
            return "Failed to update the announcement setting. Please try again."
        target = "this group" if is_group else "you"
        if subscribed:
            if previous is False:
                return f"Announcements were already on for {target}; nothing changed."
            return f"Announcements are on again for {target}. (/broadcast off stops them.)"
        if previous is True:
            return f"Announcements were already off for {target}; nothing changed."
        return f"Announcements are now off for {target}. (/broadcast on turns them back on.)"
    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error updating broadcast subscription: {e}", exc_info=True)
        return "Failed to update the announcement setting. Please try again."
