"""Inbound message dedup (services/dedup.py)."""

from unittest.mock import AsyncMock

import fakeredis

from ai_api.services.dedup import (
    DEDUP_TTL_SECONDS,
    claim_inbound,
    dedup_key,
    release_inbound,
)

JID = "5511999999999@s.whatsapp.net"


async def test_first_copy_claims_and_second_is_duplicate():
    redis = fakeredis.FakeAsyncRedis()
    assert await claim_inbound(redis, JID, "MSG1") is True
    assert await claim_inbound(redis, JID, "MSG1") is False


async def test_claim_expires_after_a_day():
    redis = fakeredis.FakeAsyncRedis()
    await claim_inbound(redis, JID, "MSG1")
    ttl = await redis.ttl(dedup_key(JID, "MSG1"))
    assert 0 < ttl <= DEDUP_TTL_SECONDS


async def test_same_message_id_in_another_chat_is_not_a_duplicate():
    # Telegram message ids are only unique within a chat.
    redis = fakeredis.FakeAsyncRedis()
    assert await claim_inbound(redis, "tg:1", "42") is True
    assert await claim_inbound(redis, "tg:2", "42") is True


async def test_key_is_prefixed_with_the_instance_name(monkeypatch):
    from ai_api.config import settings

    monkeypatch.setattr(settings, "service_name", "botx")
    assert dedup_key(JID, "M").startswith("dedup:botx:")


async def test_without_message_id_nothing_is_deduplicated():
    redis = AsyncMock()
    assert await claim_inbound(redis, JID, None) is True
    assert await claim_inbound(redis, JID, "") is True
    redis.set.assert_not_called()


async def test_redis_error_fails_open():
    redis = AsyncMock()
    redis.set.side_effect = ConnectionError("down")
    assert await claim_inbound(redis, JID, "MSG1") is True


async def test_release_lets_a_retry_through():
    redis = fakeredis.FakeAsyncRedis()
    await claim_inbound(redis, JID, "MSG1")
    await release_inbound(redis, JID, "MSG1")
    assert await claim_inbound(redis, JID, "MSG1") is True


async def test_release_swallows_redis_errors():
    redis = AsyncMock()
    redis.delete.side_effect = ConnectionError("down")
    await release_inbound(redis, JID, "MSG1")  # does not raise
