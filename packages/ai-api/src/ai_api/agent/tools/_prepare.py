"""Shared pieces for pydantic-ai ``prepare`` hooks (hide a tool for one run).

Hiding a tool that can't work in this chat saves its schema on every model
request and stops the model from trying it. Hide only on things that are the
same for the whole chat (the chat's kind, the client, a global flag), never on
who is speaking: the tool list sits before the history in each request, so a
list that changes between members of one group breaks the provider's prompt
cache. Per-sender rules (group admins) stay inside the tool as a refusal.
"""

from collections.abc import Awaitable, Callable

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from ..core import AgentDeps

PrepareHook = Callable[[RunContext[AgentDeps], ToolDefinition], Awaitable[ToolDefinition | None]]


def all_of(*hooks: PrepareHook) -> PrepareHook:
    """One ``prepare`` hook that shows the tool only if every hook does.

    Each hook may also rewrite the definition; the next one sees the result.
    Use it to add a condition to a tool that already has a hook instead of
    replacing that hook.
    """

    async def prepare(ctx: RunContext[AgentDeps], tool_def: ToolDefinition):
        current: ToolDefinition | None = tool_def
        for hook in hooks:
            current = await hook(ctx, current)
            if current is None:
                return None
        return current

    return prepare
