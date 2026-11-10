"""`prepare` hooks that hide tools on the app surfaces (``surfaces.py``).

A job from the ``/v1`` web API or the terminal chat has no chat client to send
through: its JID is ``web:…``/``cli:…`` (not a WhatsApp/Telegram address), or a
linked WhatsApp/Telegram JID with ``client_id="web"``, where the user is not on
WhatsApp in that turn. Tools that deliver something through a chat client
(reactions, messages, locations, contacts, group posts, later deliveries) are
hidden there, so the model never offers them.
"""

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from ...surfaces import delivers_through_chat_client
from ..core import AgentDeps


async def chat_client_only(
    ctx: RunContext[AgentDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Hide the tool unless the job came through a chat client."""
    deps = ctx.deps
    return tool_def if delivers_through_chat_client(deps.client_id, deps.whatsapp_jid) else None
