"""
Tests for the PDF processing stream: enqueue → consume → ack / retry / dead-letter.

Uses fakeredis for real stream semantics (consumer groups, pending entries,
XAUTOCLAIM, sorted-set retries); `process_pdf_document`, the DB session and the
chat-client reaction are mocked.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

from ai_api.streams import manager, pdf_consumer
from ai_api.streams.manager import (
    PDF_DEAD_LETTER_KEY,
    PDF_GROUP_NAME,
    PDF_RETRY_KEY,
    PDF_STREAM_KEY,
    enqueue_pdf_processing,
    ensure_pdf_consumer_group,
    promote_due_pdf_retries,
    read_pdf_stream_messages,
)

CHAT_JID = "5511999999999@s.whatsapp.net"


@pytest.fixture
async def redis():
    client = fakeredis.FakeAsyncRedis()
    await ensure_pdf_consumer_group(client)
    yield client
    await client.aclose()


@pytest.fixture
def settings_override(monkeypatch):
    from ai_api.config import settings

    monkeypatch.setattr(settings, "kb_max_pdf_retries", 2)
    monkeypatch.setattr(settings, "kb_retry_base_delay_seconds", 30)
    monkeypatch.setattr(settings, "kb_max_concurrent_processing", 2)
    return settings


@pytest.fixture
def status_updates(monkeypatch):
    """Capture _update_document_status calls instead of touching a DB."""
    calls: list[tuple] = []

    def _fake(document_id, status, error_message=None):
        calls.append((document_id, status, error_message))

    monkeypatch.setattr(pdf_consumer, "_update_document_status", _fake)
    return calls


@pytest.fixture
def reactions(monkeypatch):
    sent: list[str] = []

    async def _fake(fields, emoji):
        if fields.get("whatsapp_jid") and fields.get("whatsapp_message_id"):
            sent.append(emoji)

    monkeypatch.setattr(pdf_consumer, "_send_reaction", _fake)
    return sent


async def _read_one(redis):
    messages = await read_pdf_stream_messages(redis, count=1, block=10)
    assert messages, "expected a PDF job on the stream"
    _stream, entries = messages[0]
    message_id, data = entries[0]
    return message_id.decode(), data


async def _pending_count(redis) -> int:
    info = await redis.xpending(PDF_STREAM_KEY, PDF_GROUP_NAME)
    return info["pending"]


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    async def test_enqueue_writes_string_fields(self, redis):
        await enqueue_pdf_processing(
            redis,
            document_id="doc-1",
            file_path="/data/doc-1.pdf",
            whatsapp_jid=CHAT_JID,
            job_id="job-1",
            whatsapp_message_id="wamid-1",
            client_id="cloud",
        )
        _mid, data = await _read_one(redis)
        assert data[b"document_id"] == b"doc-1"
        assert data[b"file_path"] == b"/data/doc-1.pdf"
        assert data[b"retry_count"] == b"0"
        assert data[b"whatsapp_jid"] == CHAT_JID.encode()
        assert data[b"whatsapp_message_id"] == b"wamid-1"
        assert data[b"client_id"] == b"cloud"

    async def test_kb_upload_omits_chat_fields(self, redis):
        await enqueue_pdf_processing(redis, document_id="doc-1", file_path="/data/doc-1.pdf")
        _mid, data = await _read_one(redis)
        assert b"whatsapp_jid" not in data
        assert b"whatsapp_message_id" not in data
        assert b"client_id" not in data


# ---------------------------------------------------------------------------
# process_pdf_job outcomes
# ---------------------------------------------------------------------------


class TestProcessPdfJob:
    async def test_success_acks_deletes_and_reacts(
        self, redis, settings_override, status_updates, reactions
    ):
        await enqueue_pdf_processing(
            redis, "doc-1", "/data/doc-1.pdf", whatsapp_jid=CHAT_JID, whatsapp_message_id="w1"
        )
        mid, data = await _read_one(redis)

        with patch.object(
            pdf_consumer, "process_pdf_document", AsyncMock(return_value="completed")
        ) as proc:
            await pdf_consumer.process_pdf_job(redis, mid, data)

        proc.assert_awaited_once_with(
            document_id="doc-1",
            file_path="/data/doc-1.pdf",
            whatsapp_jid=CHAT_JID,
            raise_on_failure=True,
        )
        assert await _pending_count(redis) == 0
        assert await redis.xlen(PDF_STREAM_KEY) == 0  # acked AND deleted
        assert reactions == ["✅"]
        assert status_updates == []

    async def test_kb_upload_success_sends_no_reaction(self, redis, settings_override, reactions):
        await enqueue_pdf_processing(redis, "doc-1", "/data/doc-1.pdf")
        mid, data = await _read_one(redis)
        with patch.object(
            pdf_consumer, "process_pdf_document", AsyncMock(return_value="completed")
        ):
            await pdf_consumer.process_pdf_job(redis, mid, data)
        assert reactions == []
        assert await _pending_count(redis) == 0

    async def test_retriable_error_schedules_backoff_retry(
        self, redis, settings_override, status_updates, reactions
    ):
        await enqueue_pdf_processing(
            redis, "doc-1", "/data/doc-1.pdf", whatsapp_jid=CHAT_JID, whatsapp_message_id="w1"
        )
        mid, data = await _read_one(redis)

        with (
            patch.object(
                pdf_consumer, "process_pdf_document", AsyncMock(side_effect=TimeoutError())
            ),
            patch.object(pdf_consumer.time, "time", return_value=1000.0),
        ):
            await pdf_consumer.process_pdf_job(redis, mid, data)

        # Acked, parked in the retry set 30s out (base * 4**0), status back to queued
        assert await _pending_count(redis) == 0
        entries = await redis.zrange(PDF_RETRY_KEY, 0, -1, withscores=True)
        assert len(entries) == 1
        member, score = entries[0]
        assert score == 1030.0
        assert json.loads(member)["retry_count"] == "1"
        assert status_updates == [("doc-1", "queued", None)]
        assert reactions == []  # not final yet

    async def test_backoff_is_exponential(self, redis, settings_override, status_updates):
        await enqueue_pdf_processing(redis, "doc-1", "/data/doc-1.pdf", retry_count=1)
        mid, data = await _read_one(redis)
        with (
            patch.object(
                pdf_consumer, "process_pdf_document", AsyncMock(side_effect=TimeoutError())
            ),
            patch.object(pdf_consumer.time, "time", return_value=1000.0),
        ):
            await pdf_consumer.process_pdf_job(redis, mid, data)
        (_member, score), *_ = await redis.zrange(PDF_RETRY_KEY, 0, -1, withscores=True)
        assert score == 1000.0 + 30 * 4

    async def test_non_retriable_error_dead_letters(
        self, redis, settings_override, status_updates, reactions
    ):
        await enqueue_pdf_processing(
            redis, "doc-1", "/data/doc-1.pdf", whatsapp_jid=CHAT_JID, whatsapp_message_id="w1"
        )
        mid, data = await _read_one(redis)

        with patch.object(
            pdf_consumer,
            "process_pdf_document",
            AsyncMock(side_effect=ValueError("LLAMA_CLOUD_API_KEY not configured")),
        ):
            await pdf_consumer.process_pdf_job(redis, mid, data)

        assert await _pending_count(redis) == 0
        assert await redis.zcard(PDF_RETRY_KEY) == 0
        dead = await redis.xrange(PDF_DEAD_LETTER_KEY)
        assert len(dead) == 1
        assert dead[0][1][b"document_id"] == b"doc-1"
        assert b"non-retriable" in dead[0][1][b"reason"]
        assert reactions == ["❌"]

    async def test_exhausted_retries_dead_letter(
        self, redis, settings_override, status_updates, reactions
    ):
        await enqueue_pdf_processing(
            redis,
            "doc-1",
            "/data/doc-1.pdf",
            retry_count=2,  # == kb_max_pdf_retries
            whatsapp_jid=CHAT_JID,
            whatsapp_message_id="w1",
        )
        mid, data = await _read_one(redis)
        with patch.object(
            pdf_consumer, "process_pdf_document", AsyncMock(side_effect=TimeoutError())
        ):
            await pdf_consumer.process_pdf_job(redis, mid, data)

        assert await redis.zcard(PDF_RETRY_KEY) == 0
        dead = await redis.xrange(PDF_DEAD_LETTER_KEY)
        assert b"max retries exhausted" in dead[0][1][b"reason"]
        assert reactions == ["❌"]

    async def test_zero_chunks_stored_is_retried(self, redis, settings_override, status_updates):
        await enqueue_pdf_processing(redis, "doc-1", "/data/doc-1.pdf")
        mid, data = await _read_one(redis)
        with patch.object(pdf_consumer, "process_pdf_document", AsyncMock(return_value="failed")):
            await pdf_consumer.process_pdf_job(redis, mid, data)
        assert await redis.zcard(PDF_RETRY_KEY) == 1
        assert await _pending_count(redis) == 0

    async def test_partial_acks_and_reports_failure(
        self, redis, settings_override, status_updates, reactions
    ):
        await enqueue_pdf_processing(
            redis, "doc-1", "/data/doc-1.pdf", whatsapp_jid=CHAT_JID, whatsapp_message_id="w1"
        )
        mid, data = await _read_one(redis)
        with patch.object(pdf_consumer, "process_pdf_document", AsyncMock(return_value="partial")):
            await pdf_consumer.process_pdf_job(redis, mid, data)
        assert await _pending_count(redis) == 0
        assert await redis.zcard(PDF_RETRY_KEY) == 0
        assert reactions == ["❌"]

    async def test_deleted_document_is_dropped(self, redis, settings_override, reactions):
        await enqueue_pdf_processing(
            redis, "doc-1", "/data/doc-1.pdf", whatsapp_jid=CHAT_JID, whatsapp_message_id="w1"
        )
        mid, data = await _read_one(redis)
        with patch.object(pdf_consumer, "process_pdf_document", AsyncMock(return_value=None)):
            await pdf_consumer.process_pdf_job(redis, mid, data)
        assert await _pending_count(redis) == 0
        assert reactions == []

    async def test_malformed_job_dead_letters_without_processing(self, redis, settings_override):
        await redis.xadd(PDF_STREAM_KEY, {"file_path": "/x.pdf"})
        mid, data = await _read_one(redis)
        with patch.object(pdf_consumer, "process_pdf_document", AsyncMock()) as proc:
            await pdf_consumer.process_pdf_job(redis, mid, data)
        proc.assert_not_awaited()
        assert await _pending_count(redis) == 0
        dead = await redis.xrange(PDF_DEAD_LETTER_KEY)
        assert dead[0][1][b"reason"] == b"missing required fields"


# ---------------------------------------------------------------------------
# Delayed retries
# ---------------------------------------------------------------------------


class TestRetryPromotion:
    async def test_only_due_retries_are_promoted(self, redis):
        await manager.schedule_pdf_retry(
            redis, {"document_id": "due", "file_path": "/a.pdf", "retry_count": "1"}, 100.0
        )
        await manager.schedule_pdf_retry(
            redis, {"document_id": "later", "file_path": "/b.pdf", "retry_count": "1"}, 500.0
        )

        assert await promote_due_pdf_retries(redis, now=200.0) == 1

        _mid, data = await _read_one(redis)
        assert data[b"document_id"] == b"due"
        assert data[b"retry_count"] == b"1"
        assert await redis.zcard(PDF_RETRY_KEY) == 1  # "later" still parked

    async def test_retry_round_trip(self, redis, settings_override, status_updates):
        """A retriable failure comes back on the stream once its delay has passed."""
        await enqueue_pdf_processing(redis, "doc-1", "/data/doc-1.pdf")
        mid, data = await _read_one(redis)
        with (
            patch.object(
                pdf_consumer, "process_pdf_document", AsyncMock(side_effect=TimeoutError())
            ),
            patch.object(pdf_consumer.time, "time", return_value=1000.0),
        ):
            await pdf_consumer.process_pdf_job(redis, mid, data)

        assert await promote_due_pdf_retries(redis, now=1029.0) == 0
        assert await promote_due_pdf_retries(redis, now=1030.0) == 1

        mid2, data2 = await _read_one(redis)
        with patch.object(
            pdf_consumer, "process_pdf_document", AsyncMock(return_value="completed")
        ) as proc:
            await pdf_consumer.process_pdf_job(redis, mid2, data2)
        proc.assert_awaited_once()
        assert await _pending_count(redis) == 0


# ---------------------------------------------------------------------------
# Abandoned jobs (worker died mid-parse)
# ---------------------------------------------------------------------------


class TestReclaim:
    async def _abandon(self, redis, **kwargs):
        """Deliver a job to a consumer that then 'dies' without acking."""
        await enqueue_pdf_processing(redis, "doc-1", "/data/doc-1.pdf", **kwargs)
        await redis.xreadgroup(PDF_GROUP_NAME, "dead-worker", {PDF_STREAM_KEY: ">"}, count=1)
        assert await _pending_count(redis) == 1

    async def test_abandoned_job_is_reclaimed_and_retried(
        self, redis, settings_override, status_updates, monkeypatch
    ):
        await self._abandon(redis)
        monkeypatch.setattr(pdf_consumer, "_reclaim_min_idle_ms", lambda: 0)

        await pdf_consumer._maintenance(redis)

        assert await _pending_count(redis) == 0
        entries = await redis.zrange(PDF_RETRY_KEY, 0, -1)
        assert json.loads(entries[0])["retry_count"] == "1"
        assert status_updates == [("doc-1", "queued", None)]

    async def test_reclaim_loop_is_bounded(
        self, redis, settings_override, status_updates, reactions, monkeypatch
    ):
        """A PDF that keeps killing the worker (OOM) ends in the dead letter."""
        await self._abandon(redis, retry_count=2, whatsapp_jid=CHAT_JID, whatsapp_message_id="w1")
        monkeypatch.setattr(pdf_consumer, "_reclaim_min_idle_ms", lambda: 0)

        await pdf_consumer._maintenance(redis)

        assert await _pending_count(redis) == 0
        assert await redis.zcard(PDF_RETRY_KEY) == 0
        assert await redis.xlen(PDF_DEAD_LETTER_KEY) == 1
        assert status_updates == [("doc-1", "failed", pdf_consumer.INTERRUPTED_ERROR)]
        assert reactions == ["❌"]

    async def test_recent_pending_job_is_not_stolen(self, redis, settings_override):
        await self._abandon(redis)
        # Default threshold is minutes; a just-delivered job is left alone.
        await pdf_consumer._maintenance(redis)
        assert await _pending_count(redis) == 1
        assert await redis.zcard(PDF_RETRY_KEY) == 0

    def test_reclaim_threshold_exceeds_processing_timeout(self, settings_override):
        assert (
            pdf_consumer._reclaim_min_idle_ms()
            > settings_override.kb_processing_timeout_seconds * 1000
        )


# ---------------------------------------------------------------------------
# Consumer loop
# ---------------------------------------------------------------------------


class TestRunPdfConsumer:
    async def test_processes_jobs_with_bounded_concurrency(
        self, redis, settings_override, monkeypatch
    ):
        monkeypatch.setattr(settings_override, "kb_max_concurrent_processing", 2)

        async def _fast_read(r, count=1, block=5000):
            return await read_pdf_stream_messages(r, count=count, block=10)

        monkeypatch.setattr(pdf_consumer, "read_pdf_stream_messages", _fast_read)

        running = 0
        peak = 0
        processed: list[str] = []

        async def _fake_process(document_id, file_path, whatsapp_jid=None, raise_on_failure=False):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            processed.append(document_id)
            return "completed"

        monkeypatch.setattr(pdf_consumer, "process_pdf_document", _fake_process)

        for i in range(5):
            await enqueue_pdf_processing(redis, f"doc-{i}", f"/data/doc-{i}.pdf")

        task = asyncio.create_task(pdf_consumer.run_pdf_consumer(redis))
        try:
            for _ in range(200):
                if len(processed) == 5:
                    break
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert sorted(processed) == [f"doc-{i}" for i in range(5)]
        assert peak == 2
        assert await _pending_count(redis) == 0
        assert await redis.xlen(PDF_STREAM_KEY) == 0

    async def test_cancel_leaves_in_flight_job_pending(self, redis, settings_override, monkeypatch):
        async def _fast_read(r, count=1, block=5000):
            return await read_pdf_stream_messages(r, count=count, block=10)

        monkeypatch.setattr(pdf_consumer, "read_pdf_stream_messages", _fast_read)
        started = asyncio.Event()

        async def _slow_process(**_kwargs):
            started.set()
            await asyncio.sleep(10)

        monkeypatch.setattr(pdf_consumer, "process_pdf_document", _slow_process)
        await enqueue_pdf_processing(redis, "doc-1", "/data/doc-1.pdf")

        task = asyncio.create_task(pdf_consumer.run_pdf_consumer(redis))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        # Not acked: it will be reclaimed and retried once a worker is back.
        assert await _pending_count(redis) == 1


# ---------------------------------------------------------------------------
# Reaction + status helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    async def test_send_reaction_routes_by_client_id(self, monkeypatch):
        wa_client = MagicMock()
        wa_client.send_reaction = AsyncMock()
        factory = MagicMock(return_value=wa_client)
        monkeypatch.setattr(pdf_consumer, "create_whatsapp_client", factory)

        await pdf_consumer._send_reaction(
            {"whatsapp_jid": "tg:42", "whatsapp_message_id": "7", "client_id": "telegram"}, "✅"
        )

        from ai_api.config import settings

        assert factory.call_args.kwargs["base_url"] == settings.telegram_client_url
        wa_client.send_reaction.assert_awaited_once_with("tg:42", "7", "✅")

    async def test_send_reaction_never_raises(self, monkeypatch):
        wa_client = MagicMock()
        wa_client.send_reaction = AsyncMock(side_effect=RuntimeError("client down"))
        monkeypatch.setattr(
            pdf_consumer, "create_whatsapp_client", MagicMock(return_value=wa_client)
        )
        await pdf_consumer._send_reaction({"whatsapp_jid": "j", "whatsapp_message_id": "m"}, "❌")

    def test_update_document_status(self, monkeypatch):
        doc = MagicMock()
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = doc
        monkeypatch.setattr(pdf_consumer, "SessionLocal", lambda: session)

        pdf_consumer._update_document_status("doc-1", "failed", "boom")

        assert doc.status == "failed"
        assert doc.error_message == "boom"
        session.commit.assert_called_once()
        session.close.assert_called_once()


class TestConsumerGroupRecovery:
    async def test_recreates_group_after_nogroup_error(self, settings_override, monkeypatch):
        """Redis flushed (or down at startup): the loop recreates the group and resumes."""
        client = fakeredis.FakeAsyncRedis()  # no group created yet
        monkeypatch.setattr(pdf_consumer.asyncio, "sleep", AsyncMock())

        async def _fast_read(r, count=1, block=5000):
            return await read_pdf_stream_messages(r, count=count, block=10)

        monkeypatch.setattr(pdf_consumer, "read_pdf_stream_messages", _fast_read)
        # Startup creation "fails" (as if Redis were down), later calls work.
        real_ensure = pdf_consumer.ensure_pdf_consumer_group
        calls = {"n": 0}

        async def _flaky_ensure(r):
            calls["n"] += 1
            if calls["n"] == 1:
                return  # logged-and-swallowed failure at startup
            await real_ensure(r)

        monkeypatch.setattr(pdf_consumer, "ensure_pdf_consumer_group", _flaky_ensure)
        done = asyncio.Event()

        async def _process(**_kwargs):
            done.set()
            return "completed"

        monkeypatch.setattr(pdf_consumer, "process_pdf_document", _process)

        task = asyncio.create_task(pdf_consumer.run_pdf_consumer(client))
        try:
            await asyncio.sleep(0.05)
            await enqueue_pdf_processing(client, "doc-1", "/data/doc-1.pdf")
            await asyncio.wait_for(done.wait(), timeout=3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()
        assert calls["n"] >= 2
