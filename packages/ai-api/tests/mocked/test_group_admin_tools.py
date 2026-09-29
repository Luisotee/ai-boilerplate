"""Group-changing agent tools need a confirmed group admin, like the slash commands.

Without the gate any member could ask the bot in plain words to do what
/tts, /stt, /memories clear and /clean reserve for admins. Fails closed:
anything but an explicit is_group_admin=True is refused.
"""

from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.tools._group import group_admin_refusal
from ai_api.agent.tools.settings import update_stt_settings, update_tts_settings

GROUPS = ["120363012345678@g.us", "tg:-1001234567890"]


def _ctx(jid, is_admin):
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = jid
    ctx.deps.is_group_admin = is_admin
    return ctx


class TestGroupAdminRefusal:
    @pytest.mark.parametrize("jid", GROUPS)
    @pytest.mark.parametrize("is_admin", [None, False])
    def test_refuses_non_admins_in_groups(self, jid, is_admin):
        refusal = group_admin_refusal(_ctx(jid, is_admin).deps, "do it", "/cmd")
        assert refusal == "Only a group admin can do it. An admin can ask me, or send /cmd."

    @pytest.mark.parametrize("jid", GROUPS)
    def test_allows_admins(self, jid):
        assert group_admin_refusal(_ctx(jid, True).deps, "do it", "/cmd") is None

    @pytest.mark.parametrize("jid", ["5511999999999@s.whatsapp.net", "tg:42"])
    def test_private_chats_are_never_gated(self, jid):
        assert group_admin_refusal(_ctx(jid, None).deps, "do it", "/cmd") is None


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: update_tts_settings(ctx, enabled=True),
        lambda ctx: update_stt_settings(ctx, language="pt"),
    ],
    ids=["tts", "stt"],
)
class TestSettingsToolsInGroups:
    async def test_non_admin_is_refused_and_nothing_changes(self, call):
        ctx = _ctx(GROUPS[0], None)
        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as prefs:
            result = await call(ctx)
        assert "Only a group admin" in result
        prefs.assert_not_called()
        ctx.deps.db.commit.assert_not_called()

    async def test_admin_may_change_them(self, call):
        ctx = _ctx(GROUPS[1], True)
        with patch("ai_api.agent.tools.settings.get_or_create_preferences") as prefs:
            prefs.return_value = MagicMock(tts_enabled=False, tts_language="en", stt_language=None)
            result = await call(ctx)
        assert "Only a group admin" not in result
        ctx.deps.db.commit.assert_called_once()
