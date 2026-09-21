"""Chat PDF attachments: the processor enqueues them instead of parsing inline."""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

JID = "5511999999999@s.whatsapp.net"


async def _run(enqueue_mock):
    from ai_api.streams import processor as proc_module

    mock_redis = AsyncMock()

    @asynccontextmanager
    async def mock_get_redis_client():
        yield mock_redis

    seen_messages: list[str] = []

    async def agent(message, *_args, **_kwargs):
        seen_messages.append(message)
        yield "Got it."

    whatsapp_client = AsyncMock()
    assistant_msg = MagicMock()
    assistant_msg.id = uuid.uuid4()

    with (
        patch.object(proc_module, "SessionLocal", return_value=MagicMock()),
        patch.object(proc_module, "get_conversation_history", return_value=[]),
        patch.object(proc_module, "create_embedding_service", MagicMock(return_value=None)),
        patch.object(proc_module, "create_whatsapp_client", return_value=whatsapp_client),
        patch.object(proc_module, "get_ai_response", agent),
        patch.object(proc_module, "get_redis_client", mock_get_redis_client),
        patch.object(proc_module, "enqueue_pdf_processing", enqueue_mock),
        patch.object(proc_module, "save_message", return_value=assistant_msg) as save_message,
        patch.object(proc_module, "save_job_chunk", new_callable=AsyncMock) as save_chunk,
        patch.object(proc_module, "set_job_metadata", new_callable=AsyncMock),
    ):
        result = await proc_module.process_chat_job_direct(
            user_id=str(uuid.uuid4()),
            whatsapp_jid=JID,
            message="[document]",
            conversation_type="private",
            user_message_id=str(uuid.uuid4()),
            job_id="job-1",
            whatsapp_message_id="wamid-1",
            has_document=True,
            document_id="doc-1",
            document_path="/data/doc-1.pdf",
            document_filename="report.pdf",
            client_id="cloud",
        )
    return result, mock_redis, seen_messages, whatsapp_client, save_chunk, save_message


class TestChatDocumentEnqueue:
    @pytest.mark.asyncio
    async def test_document_is_enqueued_not_parsed(self):
        enqueue = AsyncMock(return_value="1-0")
        result, redis, seen, wa, _chunk, _save = await _run(enqueue)

        enqueue.assert_awaited_once_with(
            redis=redis,
            document_id="doc-1",
            file_path="/data/doc-1.pdf",
            whatsapp_jid=JID,
            job_id="job-1",
            whatsapp_message_id="wamid-1",
            client_id="cloud",
        )
        wa.send_reaction.assert_awaited_once_with(JID, "wamid-1", "⏳")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_agent_is_told_the_document_is_not_ready(self):
        _result, _redis, seen, *_ = await _run(AsyncMock(return_value="1-0"))
        assert len(seen) == 1
        assert "report.pdf" in seen[0]
        assert "not searchable yet" in seen[0]

    @pytest.mark.asyncio
    async def test_enqueue_failure_replies_with_error_and_skips_agent(self):
        enqueue = AsyncMock(side_effect=ConnectionError("redis down"))
        result, _redis, seen, wa, save_chunk, save_message = await _run(enqueue)

        assert result == {"success": False, "job_id": "job-1", "error": "document_enqueue_failed"}
        assert seen == []  # agent never ran
        wa.send_reaction.assert_any_await(JID, "wamid-1", "❌")
        reply = save_chunk.await_args.args[3]
        assert "report.pdf" in reply
        assert "redis down" not in reply
