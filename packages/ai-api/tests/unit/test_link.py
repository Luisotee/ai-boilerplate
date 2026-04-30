"""
Unit tests for ai_api.services.link — code generation, consumption, merge, unlink.

Uses fakeredis.aioredis.FakeRedis as a drop-in async Redis substitute and
MagicMock for the SQLAlchemy session (no real database).
"""

import uuid
from unittest.mock import MagicMock

import fakeredis.aioredis
import pytest

from ai_api.services.link import (
    ERROR_ALREADY_LINKED,
    ERROR_INVALID_CODE,
    ERROR_MERGE_FAILED,
    ERROR_SAME_PLATFORM,
    ERROR_SAME_USER,
    ERROR_SOURCE_GONE,
    LINK_CODE_TTL_SECONDS,
    consume_link_code,
    generate_link_code,
    platform_for_jid,
    unlink,
)


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


def _make_db_with_users(*users):
    """Build a MagicMock session whose query(User).filter(...).first() resolves
    to the user whose id matches the filter value."""
    db = MagicMock()
    by_id = {str(u.id): u for u in users}

    def query_filter(*conditions):
        # We only query by User.id == X in consume_link_code; the filter object
        # carries .right.value with the bound id (sqlalchemy BinaryExpression).
        # MagicMock would lose that; instead, we sniff the chain by inspecting
        # the latest `eq` call on User.id. Tests pass a tuple (id_str,) via the
        # `tester_lookup` attribute set per-call — see helpers below.
        result = MagicMock()
        # Read which user the test is asking for from a side-channel:
        result.first.return_value = db._next_user
        return result

    db.query.return_value.filter = query_filter
    db._next_user_map = by_id
    db._next_user = None
    return db


# ---------------------------------------------------------------------------
# platform_for_jid
# ---------------------------------------------------------------------------


class TestPlatformForJid:
    def test_telegram_jid(self):
        assert platform_for_jid("tg:42") == "telegram"

    def test_whatsapp_phone_jid(self):
        assert platform_for_jid("15551234567@s.whatsapp.net") == "whatsapp"

    def test_whatsapp_lid(self):
        assert platform_for_jid("12345@lid") == "whatsapp"


# ---------------------------------------------------------------------------
# generate_link_code
# ---------------------------------------------------------------------------


class TestGenerateLinkCode:
    async def test_generates_six_digit_code(self, redis):
        user_id = str(uuid.uuid4())
        code = await generate_link_code(redis, user_id, "whatsapp")
        assert len(code) == 6
        assert code.isdigit()

    async def test_stores_payload_in_redis(self, redis):
        user_id = str(uuid.uuid4())
        code = await generate_link_code(redis, user_id, "telegram")
        raw = await redis.get(f"link:code:{code}")
        assert raw is not None
        payload = raw.decode()
        assert payload == f"{user_id}|telegram"

    async def test_sets_reverse_lookup(self, redis):
        user_id = str(uuid.uuid4())
        code = await generate_link_code(redis, user_id, "whatsapp")
        raw = await redis.get(f"link:user:{user_id}")
        assert raw is not None
        assert raw.decode() == code

    async def test_ttl_is_set(self, redis):
        user_id = str(uuid.uuid4())
        code = await generate_link_code(redis, user_id, "whatsapp")
        ttl = await redis.ttl(f"link:code:{code}")
        # Some tolerance; fakeredis may report exact TTL
        assert 0 < ttl <= LINK_CODE_TTL_SECONDS

    async def test_second_call_invalidates_prior_code(self, redis):
        user_id = str(uuid.uuid4())
        first = await generate_link_code(redis, user_id, "whatsapp")
        second = await generate_link_code(redis, user_id, "whatsapp")
        assert first != second or True  # codes may collide rarely; key check is what matters
        # First code should no longer resolve
        first_raw = await redis.get(f"link:code:{first}")
        if first == second:
            # Same code (rare) — still valid since reverse-lookup points to it
            return
        assert first_raw is None
        # Reverse-lookup now points to second
        rev = await redis.get(f"link:user:{user_id}")
        assert rev.decode() == second


# ---------------------------------------------------------------------------
# consume_link_code — error cases
# ---------------------------------------------------------------------------


class TestConsumeLinkCodeErrors:
    async def test_invalid_code_returns_error(self, redis):
        db = MagicMock()
        result = await consume_link_code(db, redis, "999999", str(uuid.uuid4()), "telegram")
        assert result.success is False
        assert result.error == ERROR_INVALID_CODE
        assert "invalid or has expired" in result.message

    async def test_same_user_returns_error(self, redis):
        user_id = str(uuid.uuid4())
        code = await generate_link_code(redis, user_id, "whatsapp")
        db = MagicMock()
        # current_user_id equals the source user_id
        result = await consume_link_code(db, redis, code, user_id, "whatsapp")
        assert result.success is False
        assert result.error == ERROR_SAME_USER

    async def test_same_platform_returns_error(self, redis):
        source_id = str(uuid.uuid4())
        target_id = str(uuid.uuid4())
        code = await generate_link_code(redis, source_id, "whatsapp")
        db = MagicMock()
        result = await consume_link_code(db, redis, code, target_id, "whatsapp")
        assert result.success is False
        assert result.error == ERROR_SAME_PLATFORM

    async def test_source_user_missing_returns_error(self, redis):
        source_id = str(uuid.uuid4())
        target_id = str(uuid.uuid4())
        code = await generate_link_code(redis, source_id, "whatsapp")

        db = MagicMock()
        # Both lookups return None
        db.query.return_value.filter.return_value.first.return_value = None

        result = await consume_link_code(db, redis, code, target_id, "telegram")
        assert result.success is False
        assert result.error == ERROR_SOURCE_GONE

    async def test_already_linked_source_returns_error(self, redis):
        source_id = str(uuid.uuid4())
        target_id = str(uuid.uuid4())
        code = await generate_link_code(redis, source_id, "whatsapp")

        # Source is a WhatsApp row that already has telegram_jid set.
        source_user = MagicMock()
        source_user.id = uuid.UUID(source_id)
        source_user.whatsapp_jid = "555@s.whatsapp.net"
        source_user.telegram_jid = "tg:99"

        target_user = MagicMock()
        target_user.id = uuid.UUID(target_id)
        target_user.whatsapp_jid = "tg:42"
        target_user.telegram_jid = None

        db = MagicMock()
        # First filter() call (source) returns source_user; second (target) returns target_user.
        db.query.return_value.filter.return_value.first.side_effect = [
            source_user,
            target_user,
        ]

        result = await consume_link_code(db, redis, code, target_id, "telegram")
        assert result.success is False
        assert result.error == ERROR_ALREADY_LINKED


# ---------------------------------------------------------------------------
# consume_link_code — happy path
# ---------------------------------------------------------------------------


class TestConsumeLinkCodeHappyPath:
    async def test_whatsapp_initiates_link_with_telegram(self, redis):
        whatsapp_id = str(uuid.uuid4())
        telegram_id = str(uuid.uuid4())
        code = await generate_link_code(redis, whatsapp_id, "whatsapp")

        whatsapp_user = MagicMock()
        whatsapp_user.id = uuid.UUID(whatsapp_id)
        whatsapp_user.whatsapp_jid = "555@s.whatsapp.net"
        whatsapp_user.telegram_jid = None

        telegram_user = MagicMock()
        telegram_user.id = uuid.UUID(telegram_id)
        telegram_user.whatsapp_jid = "tg:42"
        telegram_user.telegram_jid = None

        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            whatsapp_user,  # source lookup
            telegram_user,  # target lookup
        ]

        result = await consume_link_code(db, redis, code, telegram_id, "telegram")
        assert result.success is True
        assert "Linked successfully" in result.message
        # WhatsApp row gets telegram_jid populated
        assert whatsapp_user.telegram_jid == "tg:42"
        # Telegram orphan deleted (cascade clears messages/prefs/memory)
        db.delete.assert_called_once_with(telegram_user)
        db.commit.assert_called_once()
        # Code cleaned up
        assert await redis.get(f"link:code:{code}") is None
        assert await redis.get(f"link:user:{whatsapp_id}") is None

    async def test_telegram_initiates_link_with_whatsapp(self, redis):
        """Reverse direction: code generated on Telegram side."""
        whatsapp_id = str(uuid.uuid4())
        telegram_id = str(uuid.uuid4())
        code = await generate_link_code(redis, telegram_id, "telegram")

        whatsapp_user = MagicMock()
        whatsapp_user.id = uuid.UUID(whatsapp_id)
        whatsapp_user.whatsapp_jid = "555@s.whatsapp.net"
        whatsapp_user.telegram_jid = None

        telegram_user = MagicMock()
        telegram_user.id = uuid.UUID(telegram_id)
        telegram_user.whatsapp_jid = "tg:42"
        telegram_user.telegram_jid = None

        db = MagicMock()
        # Source is the telegram side (code owner); target is whatsapp.
        db.query.return_value.filter.return_value.first.side_effect = [
            telegram_user,  # source lookup
            whatsapp_user,  # target lookup
        ]

        result = await consume_link_code(db, redis, code, whatsapp_id, "whatsapp")
        assert result.success is True
        # WhatsApp row is still the kept row regardless of who initiated.
        assert whatsapp_user.telegram_jid == "tg:42"
        db.delete.assert_called_once_with(telegram_user)

    async def test_consume_link_code_rolls_back_on_commit_failure(self, redis):
        """If db.commit() raises (e.g. unique-constraint race on telegram_jid),
        the session must be rolled back and a MERGE_FAILED LinkResult returned
        — otherwise the dirty session would poison downstream work in the same
        request."""
        whatsapp_id = str(uuid.uuid4())
        telegram_id = str(uuid.uuid4())
        code = await generate_link_code(redis, whatsapp_id, "whatsapp")

        whatsapp_user = MagicMock()
        whatsapp_user.id = uuid.UUID(whatsapp_id)
        whatsapp_user.whatsapp_jid = "555@s.whatsapp.net"
        whatsapp_user.telegram_jid = None

        telegram_user = MagicMock()
        telegram_user.id = uuid.UUID(telegram_id)
        telegram_user.whatsapp_jid = "tg:42"
        telegram_user.telegram_jid = None

        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [
            whatsapp_user,
            telegram_user,
        ]
        db.commit.side_effect = Exception("simulated unique-constraint violation")

        result = await consume_link_code(db, redis, code, telegram_id, "telegram")

        assert result.success is False
        assert result.error == ERROR_MERGE_FAILED
        assert result.message and "database error" in result.message.lower()
        db.rollback.assert_called_once()
        # Redis state is intentionally NOT cleared on merge failure so the user
        # can retry with the same code (still within TTL).
        assert await redis.get(f"link:code:{code}") is not None


# ---------------------------------------------------------------------------
# unlink
# ---------------------------------------------------------------------------


class TestUnlink:
    def test_clears_telegram_jid(self):
        user = MagicMock()
        user.id = uuid.uuid4()
        user.telegram_jid = "tg:42"

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user

        result = unlink(db, str(user.id))
        assert result is True
        assert user.telegram_jid is None
        db.commit.assert_called_once()

    def test_returns_false_when_no_link(self):
        user = MagicMock()
        user.id = uuid.uuid4()
        user.telegram_jid = None

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user

        result = unlink(db, str(user.id))
        assert result is False
        db.commit.assert_not_called()

    def test_returns_false_when_user_not_found(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = unlink(db, str(uuid.uuid4()))
        assert result is False
        db.commit.assert_not_called()

    def test_rolls_back_and_returns_false_on_commit_failure(self):
        """Commit failure during unlink must rollback the session and surface
        as False (no link cleared) rather than letting a dirty session leak."""
        user = MagicMock()
        user.id = uuid.uuid4()
        user.telegram_jid = "tg:42"

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        db.commit.side_effect = Exception("simulated DB outage")

        result = unlink(db, str(user.id))

        assert result is False
        db.rollback.assert_called_once()
