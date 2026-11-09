"""Tools that deliver through a chat client are hidden on the app surfaces."""

from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.core import agent
from ai_api.agent.tools._surface import chat_client_only, not_app_only_chat
from ai_api.agent.tools.group_context import shared_group_tools_enabled

CHAT_CLIENT_TOOLS = (
    "send_whatsapp_reaction",
    "send_whatsapp_location",
    "send_whatsapp_contact",
    "send_whatsapp_message",
)


def _ctx(client_id, jid):
    ctx = MagicMock()
    ctx.deps.client_id, ctx.deps.whatsapp_jid = client_id, jid
    return ctx


def test_whatsapp_tools_carry_the_hook():
    tools = agent._function_toolset.tools
    for name in CHAT_CLIENT_TOOLS:
        assert tools[name].prepare is chat_client_only
    assert tools["set_broadcast_subscription"].prepare is not_app_only_chat


@pytest.mark.parametrize(
    "client_id,jid,visible",
    [
        (None, "5511999999999@s.whatsapp.net", True),
        ("telegram", "tg:42", True),
        ("web", "5511999999999@s.whatsapp.net", False),
        (None, "web:abc", False),
        (None, "cli:luis", False),
    ],
)
async def test_chat_client_only(client_id, jid, visible):
    tool_def = MagicMock()
    result = await chat_client_only(_ctx(client_id, jid), tool_def)
    assert (result is tool_def) is visible


async def test_broadcast_subscription_hidden_only_for_app_rows():
    tool_def = MagicMock()
    # A linked WhatsApp user on the web can still opt out of broadcasts.
    assert await not_app_only_chat(_ctx("web", "5511@s.whatsapp.net"), tool_def) is tool_def
    assert await not_app_only_chat(_ctx(None, "web:abc"), tool_def) is None
    assert await not_app_only_chat(_ctx(None, "cli:luis"), tool_def) is None


async def test_shared_group_tools_hidden_on_web_even_when_enabled():
    tool_def = MagicMock()
    with patch("ai_api.agent.tools.group_context.runtime_config") as rc:
        rc.get.return_value = True
        assert (
            await shared_group_tools_enabled(_ctx("web", "5511@s.whatsapp.net"), tool_def) is None
        )
        assert (
            await shared_group_tools_enabled(_ctx(None, "5511@s.whatsapp.net"), tool_def)
            is tool_def
        )
