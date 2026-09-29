"""Group-admin gate shared by the tools that change a whole group's state."""

from ...database import is_group_jid
from ...logger import logger
from ..core import AgentDeps


def group_admin_refusal(deps: AgentDeps, what: str, command: str) -> str | None:
    """Refusal text if this run may not change ``what`` for the group; None if it may.

    Same rule as ADMIN_ONLY_COMMANDS: in a group (detected by the conversation's
    own JID) only a confirmed admin may change group-wide state, and it FAILS
    CLOSED — anything but an explicit True (unknown, lookup failed, older
    client) is refused. Otherwise any member could ask the bot in plain words
    to do what the slash command reserves for admins.
    """
    if not is_group_jid(deps.whatsapp_jid) or deps.is_group_admin is True:
        return None
    logger.info("Tool refused in group %s: sender is not a confirmed admin", deps.whatsapp_jid)
    return f"Only a group admin can {what}. An admin can ask me, or send {command}."
