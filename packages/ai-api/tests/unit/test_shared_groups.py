"""Unit tests for services/shared_groups.py — per-platform shared-group resolution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from ai_api.services.shared_groups import (
    MAX_DERIVED_GROUPS,
    derive_telegram_shared_groups,
    platform_of,
    resolve_shared_groups,
    telegram_identity,
)
from ai_api.whatsapp.client import SharedGroup
from tests.helpers.factories import make_user


class TestTelegramIdentity:
    def test_linked_user_uses_telegram_jid(self):
        assert telegram_identity(make_user("5511@s.whatsapp.net", telegram_jid="tg:5")) == "tg:5"

    def test_unlinked_telegram_user_uses_whatsapp_jid(self):
        assert telegram_identity(make_user("tg:5", telegram_jid=None)) == "tg:5"

    def test_whatsapp_only_user_has_none(self):
        assert telegram_identity(make_user("5511@s.whatsapp.net", telegram_jid=None)) is None


class TestPlatformOf:
    def test_none_and_unknown_mean_baileys(self):
        assert platform_of(None) == "baileys"
        assert platform_of("baileys") == "baileys"

    def test_cloud_and_telegram(self):
        assert platform_of("cloud") == "cloud"
        assert platform_of("telegram") == "telegram"


class TestDeriveTelegramSharedGroups:
    def test_no_identity_skips_the_query(self):
        db = MagicMock()
        assert derive_telegram_shared_groups(db, make_user("5511@s.whatsapp.net")) == []
        db.execute.assert_not_called()

    def test_rows_become_shared_groups_with_jid_fallback_subject(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = [("tg:-1", "Team"), ("tg:-2", None)]
        groups = derive_telegram_shared_groups(db, make_user("tg:5"))
        assert groups == [
            SharedGroup(group_jid="tg:-1", subject="Team"),
            SharedGroup(group_jid="tg:-2", subject="tg:-2"),
        ]

    def test_query_is_scoped_to_the_requester_identity(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        derive_telegram_shared_groups(db, make_user("tg:5"))
        sql = str(db.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "sender_jid = 'tg:5'" in sql
        assert "conversation_type = 'group'" in sql
        assert f"LIMIT {MAX_DERIVED_GROUPS}" in sql


class TestResolveSharedGroups:
    def _deps(self, client_id):
        client = MagicMock()
        client.get_shared_groups = AsyncMock(return_value=[SharedGroup("1@g.us", "A")])
        return SimpleNamespace(client_id=client_id, db=MagicMock(), whatsapp_client=client)

    async def test_cloud_has_no_groups_and_makes_no_call(self):
        deps = self._deps("cloud")
        assert await resolve_shared_groups(deps, make_user("5511@s.whatsapp.net")) == []
        deps.whatsapp_client.get_shared_groups.assert_not_called()

    async def test_baileys_passes_db_identifiers(self):
        deps = self._deps(None)
        user = make_user("5511@s.whatsapp.net", whatsapp_lid="9@lid", phone="+5511")
        result = await resolve_shared_groups(deps, user)
        assert result == [SharedGroup("1@g.us", "A")]
        deps.whatsapp_client.get_shared_groups.assert_awaited_once_with(
            jid="5511@s.whatsapp.net", lid="9@lid", phone="+5511"
        )

    async def test_telegram_derives_from_the_db(self):
        deps = self._deps("telegram")
        with patch(
            "ai_api.services.shared_groups.derive_telegram_shared_groups", return_value=[]
        ) as derive:
            await resolve_shared_groups(deps, make_user("tg:5"))
        derive.assert_called_once()
        deps.whatsapp_client.get_shared_groups.assert_not_called()
