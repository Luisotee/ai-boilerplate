"""Late reply delivery (streams/delivery.py) and its use in the processor,
the consumer (expired jobs) and GET /chat/job."""

import time
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

from ai_api.streams import delivery
from ai_api.streams.delivery import (
    claim_delivery,
    deliver_if_late,
    is_expired,
    is_late,
    strip_burst_delimiters,
)

JID = "5511999999999@s.whatsapp.net"


def _ms_ago(seconds: float) -> int:
    return int((time.time() - seconds) * 1000)


@pytest.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis()
    yield client
    await client.aclose()


class TestTiming:
    def test_late_near_the_end_of_the_polling_window(self):
        assert not is_late(_ms_ago(30))
        assert is_late(_ms_ago(115))  # 120 s budget, 10 s margin
        assert not is_late(None)

    def test_expired_after_the_max_age(self):
        assert not is_expired(_ms_ago(600))
        assert is_expired(_ms_ago(3700))
        assert not is_expired(None)


class TestClaim:
    async def test_first_claimer_wins_and_keeps_winning(self, redis):
        assert await claim_delivery(redis, "j1", "poll") == "poll"
        assert await claim_delivery(redis, "j1", "push") == "poll"
        assert await claim_delivery(redis, "j1", "poll") == "poll"  # repeated polls


class TestDeliverIfLate:
    async def test_late_reply_is_pushed_once(self, redis):
        client = AsyncMock()
        assert await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(130))
        assert not await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(130))
        client.send_text.assert_awaited_once_with(JID, "hello")

    async def test_on_time_reply_is_left_to_the_poller(self, redis):
        client = AsyncMock()
        assert not await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(5))
        client.send_text.assert_not_called()
        assert await redis.get(delivery.claim_key("j1")) is None

    async def test_no_push_when_the_poller_already_took_it(self, redis):
        client = AsyncMock()
        await claim_delivery(redis, "j1", "poll")
        assert not await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(130))
        client.send_text.assert_not_called()

    async def test_push_failure_is_swallowed(self, redis):
        client = AsyncMock()
        client.send_text.side_effect = RuntimeError("client down")
        assert not await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(130))

    async def test_burst_delimiters_are_dropped(self, redis):
        client = AsyncMock()
        await deliver_if_late(redis, client, "j1", JID, "one\n---\ntwo", _ms_ago(130))
        client.send_text.assert_awaited_once_with(JID, "one\n\ntwo")


class TestLateReplyImages:
    """A pushed reply carries the images listed in the job metadata."""

    MEDIA = [
        {"index": 0, "caption": "Map", "mimetype": "image/png", "url": "https://x.example/a.png"},
        {"index": 1, "caption": "", "mimetype": "image/jpeg", "url": "https://x.example/b.jpg"},
    ]

    async def _job(self, redis, media):
        from ai_api.queue.utils import save_job_media, set_job_metadata

        await set_job_metadata(redis, "j1", {"status": "complete", "media": media})
        await save_job_media(redis, "j1", 0, b"png")

    async def test_images_follow_the_text(self, redis):
        await self._job(redis, self.MEDIA)
        client = AsyncMock()
        assert await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(130))
        client.send_image.assert_awaited_once_with(JID, b"png", "image/png", "Map")
        # Image 1's bytes are gone: its link goes as text instead.
        assert [c.args for c in client.send_text.await_args_list] == [
            (JID, "hello"),
            (JID, "https://x.example/b.jpg"),
        ]

    async def test_images_only_reply_is_pushed(self, redis):
        await self._job(redis, self.MEDIA[:1])
        client = AsyncMock()
        assert await deliver_if_late(redis, client, "j1", JID, "", _ms_ago(130))
        client.send_text.assert_not_called()
        client.send_image.assert_awaited_once()

    async def test_failed_image_send_falls_back_to_its_link(self, redis):
        await self._job(redis, self.MEDIA[:1])
        client = AsyncMock()
        client.send_image.side_effect = RuntimeError("no send-image route")
        assert await deliver_if_late(redis, client, "j1", JID, "hello", _ms_ago(130))
        assert client.send_text.await_args_list[-1].args == (JID, "Map: https://x.example/a.png")


def test_strip_burst_delimiters_keeps_code_fences():
    text = "a\n---\nb\n```\n---\n```"
    assert strip_burst_delimiters(text) == "a\n\nb\n```\n---\n```"


class TestProcessorPushesLateReplies:
    async def _run(self, enqueued_at_ms):
        from ai_api.streams import processor as proc_module

        fake = fakeredis.FakeAsyncRedis()

        @asynccontextmanager
        async def get_client():
            yield fake

        async def agent(*_a, **_kw):
            yield "the answer"

        wa = AsyncMock()
        with (
            patch.object(proc_module, "SessionLocal", return_value=MagicMock()),
            patch.object(proc_module, "get_conversation_history", return_value=[]),
            patch.object(proc_module, "create_embedding_service", return_value=None),
            patch.object(proc_module, "create_whatsapp_client", return_value=wa),
            patch.object(proc_module, "get_ai_response", agent),
            patch.object(proc_module, "get_redis_client", get_client),
            patch.object(proc_module, "save_message", return_value=MagicMock(id=uuid.uuid4())),
        ):
            await proc_module.process_chat_job_direct(
                user_id="u",
                whatsapp_jid=JID,
                message="q",
                conversation_type="private",
                user_message_id=str(uuid.uuid4()),
                job_id="job-late",
                enqueued_at_ms=enqueued_at_ms,
            )
        return wa, fake

    async def test_reply_after_the_window_is_pushed(self):
        wa, fake = await self._run(_ms_ago(200))
        wa.send_text.assert_awaited_once_with(JID, "the answer")
        assert await fake.get(delivery.claim_key("job-late")) == b"push"

    async def test_reply_within_the_window_is_not_pushed(self):
        wa, _fake = await self._run(_ms_ago(3))
        wa.send_text.assert_not_called()


class TestExpiredJobs:
    @pytest.fixture(autouse=True)
    def _no_burst_merging(self, monkeypatch):
        from ai_api.config import settings

        monkeypatch.setattr(settings, "chat_burst_quiet_ms", 0)

    async def test_expired_entry_is_not_run_and_gets_a_notice(self, redis):
        from ai_api.streams import consumer as consumer_module

        stream = "stream:user:u1"
        await redis.xgroup_create(stream, "workers", id="0", mkstream=True)
        old_id = f"{_ms_ago(7200)}-0"
        await redis.xadd(
            stream,
            {
                "job_id": "old-job",
                "user_id": "u1",
                "whatsapp_jid": JID,
                "message": "hello?",
                "conversation_type": "private",
                "user_message_id": "m1",
            },
            id=old_id,
        )

        async def read(_r, user_id, count=1, block=5000):
            return await redis.xreadgroup("workers", "me", {stream: ">"}, count=count)

        with (
            patch.object(consumer_module, "read_stream_messages", read),
            patch.object(consumer_module, "process_single_message", AsyncMock()) as run,
            patch.object(consumer_module, "push_text_once", AsyncMock()) as push,
        ):
            await consumer_module.process_user_stream(redis, "u1", {"running": True})

        run.assert_not_called()
        push.assert_awaited_once()
        assert push.await_args.args[4] == delivery.EXPIRED_JOB_NOTICE
        assert await redis.get("job:meta:old-job") is not None
        assert (await redis.xpending(stream, "workers"))["pending"] == 0

    async def test_expired_scheduled_entry_still_runs(self, redis):
        from ai_api.streams import consumer as consumer_module

        stream = "stream:user:u1"
        await redis.xgroup_create(stream, "workers", id="0", mkstream=True)
        await redis.xadd(
            stream,
            {"trigger": "scheduled", "run_id": "r", "job_id": "s", "user_id": "u1"},
            id=f"{_ms_ago(7200)}-0",
        )

        async def read(_r, user_id, count=1, block=5000):
            return await redis.xreadgroup("workers", "me", {stream: ">"}, count=count)

        with (
            patch.object(consumer_module, "read_stream_messages", read),
            patch.object(consumer_module, "process_single_message", AsyncMock()) as run,
        ):
            await consumer_module.process_user_stream(redis, "u1", {"running": True})

        run.assert_awaited_once()


class TestJobStatusSilent:
    async def _get(self, fake, job_id):
        from httpx import ASGITransport, AsyncClient

        from ai_api.main import app

        @asynccontextmanager
        async def get_client():
            yield fake

        with (
            patch("ai_api.main.init_db"),
            patch("ai_api.main.get_arq_redis", new_callable=AsyncMock),
            patch("ai_api.main.cleanup_expired_documents"),
            patch("ai_api.routes.chat.get_redis_client", get_client),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                resp = await c.get(f"/chat/job/{job_id}", headers={"X-API-Key": "test-api-key"})
        return resp.json()

    async def test_pushed_job_reads_silent(self, redis):
        from ai_api.queue.utils import save_job_chunk, set_job_metadata

        await save_job_chunk(redis, "j", 0, "answer")
        await set_job_metadata(redis, "j", {"total_chunks": 1})
        await claim_delivery(redis, "j", "push")

        body = await self._get(redis, "j")
        assert body["complete"] is True and body["silent"] is True
        assert body["full_response"] is None

    async def test_polled_job_blocks_a_later_push(self, redis):
        from ai_api.queue.utils import save_job_chunk, set_job_metadata

        await save_job_chunk(redis, "j", 0, "answer")
        await set_job_metadata(redis, "j", {"total_chunks": 1})

        body = await self._get(redis, "j")
        assert body["full_response"] == "answer" and body["silent"] is False
        assert await claim_delivery(redis, "j", "push") == "poll"

    async def test_queued_job_claims_nothing(self, redis):
        body = await self._get(redis, "j")
        assert body["status"] == "queued"
        assert await redis.get(delivery.claim_key("j")) is None
