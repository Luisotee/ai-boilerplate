"""prepare hooks that hide tools which can't work in this chat (agent/tools/_prepare.py)."""

from unittest.mock import MagicMock

from ai_api.agent.core import agent
from ai_api.agent.tools._prepare import all_of
from ai_api.agent.tools.whatsapp import has_message_to_react_to


async def _keep(ctx, tool_def):
    return tool_def


async def _hide(ctx, tool_def):
    return None


async def _rename(ctx, tool_def):
    return MagicMock(name=f"renamed-{tool_def}")


class TestAllOf:
    async def test_shows_only_when_every_hook_does(self):
        tool_def = MagicMock()
        assert await all_of(_keep, _keep)(MagicMock(), tool_def) is tool_def
        assert await all_of(_keep, _hide)(MagicMock(), tool_def) is None
        assert await all_of(_hide, _keep)(MagicMock(), tool_def) is None

    async def test_each_hook_sees_the_previous_rewrite(self):
        seen = []

        async def record(ctx, tool_def):
            seen.append(tool_def)
            return tool_def

        rewritten = await all_of(_rename, record)(MagicMock(), "original")
        assert seen == [rewritten]


class TestReactionNeedsAMessage:
    async def test_hidden_without_a_message_id(self):
        ctx = MagicMock()
        ctx.deps.current_message_id = None
        assert await has_message_to_react_to(ctx, MagicMock()) is None

    async def test_shown_with_a_message_id(self):
        ctx = MagicMock()
        ctx.deps.current_message_id = "ABC123"
        tool_def = MagicMock()
        assert await has_message_to_react_to(ctx, tool_def) is tool_def

    def test_registered_on_the_reaction_tool(self):
        tool = agent._function_toolset.tools["send_whatsapp_reaction"]
        assert tool.prepare is has_message_to_react_to
