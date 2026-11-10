"""/link with the web app (services/link.py): a `web:` row is always the orphan,
its API keys move to the kept WhatsApp/Telegram row, and the WA↔TG merges move
a Telegram orphan's keys too."""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import fakeredis.aioredis
import pytest

from ai_api.services import link
from ai_api.services.link import (
    ERROR_SAME_PLATFORM,
    consume_link_code,
    generate_link_code,
    link_command_reply,
    platform_for_jid,
)


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


def _user(jid, *, telegram_jid=None, opt_out=False):
    user = MagicMock(id=uuid.uuid4(), telegram_jid=telegram_jid, broadcast_opt_out=opt_out)
    user.whatsapp_jid = jid
    return user


def _db(*lookups):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = list(lookups)
    return db


def test_platform_for_web_jid():
    assert platform_for_jid("web:abc") == "web"
    assert platform_for_jid("tg:42") == "telegram"
    assert platform_for_jid("555@s.whatsapp.net") == "whatsapp"


class TestWebMerge:
    async def test_web_code_redeemed_on_whatsapp_keeps_whatsapp_row_and_moves_keys(self, redis):
        web = _user("web:abc", opt_out=True)
        wa = _user("555@s.whatsapp.net")
        code = await generate_link_code(redis, str(web.id), "web")
        db = _db(web, wa)  # source, target
        order = []
        db.delete.side_effect = lambda row: order.append(("delete", row))
        with (
            patch.object(link, "scrub_user_reports", lambda _db, uid: order.append(("scrub", uid))),
            patch.object(link, "move_api_keys", lambda _db, a, b: order.append(("move", a, b))),
        ):
            result = await consume_link_code(db, redis, code, str(wa.id), "whatsapp")
        assert result.success is True
        assert "web app now shares your WhatsApp" in result.message
        assert order == [("scrub", web.id), ("move", web.id, wa.id), ("delete", web)]
        assert wa.broadcast_opt_out is True  # an opt-out on either side survives
        db.commit.assert_called_once()
        assert await redis.get(f"link:code:{code}") is None

    async def test_whatsapp_code_redeemed_on_web(self, redis):
        wa = _user("555@s.whatsapp.net")
        web = _user("web:abc")
        code = await generate_link_code(redis, str(wa.id), "whatsapp")
        db = _db(wa, web)
        with patch.object(link, "move_api_keys") as move:
            result = await consume_link_code(db, redis, code, str(web.id), "web")
        assert result.success is True
        move.assert_called_once_with(db, web.id, wa.id)
        db.delete.assert_called_once_with(web)

    async def test_an_already_linked_whatsapp_telegram_pair_can_take_a_web_link(self, redis):
        web = _user("web:abc")
        wa = _user("555@s.whatsapp.net", telegram_jid="tg:42")
        code = await generate_link_code(redis, str(web.id), "web")
        result = await consume_link_code(db := _db(web, wa), redis, code, str(wa.id), "whatsapp")
        assert result.success is True
        assert wa.telegram_jid == "tg:42"  # untouched
        db.delete.assert_called_once_with(web)

    async def test_telegram_orphan_can_take_a_web_link(self, redis):
        web = _user("web:abc")
        tg = _user("tg:42")
        code = await generate_link_code(redis, str(web.id), "web")
        result = await consume_link_code(_db(web, tg), redis, code, str(tg.id), "telegram")
        assert result.success is True
        assert "Telegram" in result.message

    async def test_web_to_web_is_refused(self, redis):
        a, b = _user("web:a"), _user("web:b")
        code = await generate_link_code(redis, str(a.id), "web")
        result = await consume_link_code(_db(a, b), redis, code, str(b.id), "web")
        assert result.success is False
        assert result.error == ERROR_SAME_PLATFORM

    async def test_commit_failure_rolls_back_and_keeps_the_code(self, redis):
        web, wa = _user("web:abc"), _user("555@s.whatsapp.net")
        code = await generate_link_code(redis, str(web.id), "web")
        db = _db(web, wa)
        db.commit.side_effect = RuntimeError("db down")
        with patch.object(link, "scrub_user_reports"):
            result = await consume_link_code(db, redis, code, str(wa.id), "whatsapp")
        assert result.success is False
        db.rollback.assert_called_once()
        assert await redis.get(f"link:code:{code}") is not None


async def test_whatsapp_telegram_merge_moves_the_orphans_keys(redis):
    wa, tg = _user("555@s.whatsapp.net"), _user("tg:42")
    code = await generate_link_code(redis, str(wa.id), "whatsapp")
    db = _db(wa, tg)
    order = []
    db.delete.side_effect = lambda row: order.append(("delete", row))
    with (
        patch.object(link, "scrub_user_reports"),
        patch.object(link, "move_api_keys", lambda _db, a, b: order.append(("move", a, b))),
    ):
        result = await consume_link_code(db, redis, code, str(tg.id), "telegram")
    assert result.success is True
    assert order == [("move", tg.id, wa.id), ("delete", tg)]


class TestLinkCommandReply:
    @staticmethod
    def _opener(redis):
        @asynccontextmanager
        async def ctx():
            yield redis

        return ctx

    async def test_group_is_refused(self, redis):
        text = await link_command_reply(
            MagicMock(), self._opener(redis), _user("g@g.us"), "g@g.us", "group", ["/link"]
        )
        assert "only available in private chats" in text

    async def test_terminal_chat_is_refused(self, redis):
        text = await link_command_reply(
            MagicMock(), self._opener(redis), _user("cli:luis"), "cli:luis", "private", ["/link"]
        )
        assert "terminal chat" in text

    async def test_web_code_points_to_whatsapp_or_telegram(self, redis):
        user = _user("web:abc")
        text = await link_command_reply(
            MagicMock(), self._opener(redis), user, "web:abc", "private", ["/link"]
        )
        assert "On WhatsApp or Telegram, send `/link " in text
        assert "web conversation's history will be discarded" in text
        assert await redis.get(f"link:user:{user.id}") is not None

    async def test_whatsapp_code_mentions_the_web_app(self, redis):
        user = _user("555@s.whatsapp.net")
        text = await link_command_reply(
            MagicMock(), self._opener(redis), user, user.whatsapp_jid, "private", ["/link"]
        )
        assert "On Telegram (or in the web app)" in text
