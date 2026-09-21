"""Tests for the Redis Streams chat processor error handling."""

import uuid
from contextlib import asynccontextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelHTTPError,
    UnexpectedModelBehavior,
)

FALLBACK_TEXT = "Sorry, something went wrong while processing your message. Please try again."


def _make_failing_agent(exc: Exception):
    """Return an async-generator replacement for get_ai_response that raises immediately."""

    async def failing(*_args, **_kwargs):
        raise exc
        yield  # unreachable — makes this an async generator

    return failing


def _make_streaming_agent(tokens: list[str]):
    """Return an async-generator replacement for get_ai_response that yields `tokens`."""

    async def streaming(*_args, **_kwargs):
        for token in tokens:
            yield token

    return streaming


async def _run_processor_with_model_error(
    exc: Exception,
    *,
    whatsapp_message_id: str | None = "wamid-test",
):
    """
    Invoke process_chat_job_direct with get_ai_response patched to raise `exc`.

    Returns a tuple of (result, mock_save_chunk, mock_set_meta, mock_save_message,
    mock_whatsapp_client) so individual tests can assert on what was recorded.
    """
    return await _run_processor(_make_failing_agent(exc), whatsapp_message_id=whatsapp_message_id)


async def _run_processor(
    agent_fn,
    *,
    whatsapp_message_id: str | None = "wamid-test",
    raises: type[BaseException] | None = None,
):
    """Invoke process_chat_job_direct with get_ai_response replaced by `agent_fn`.

    Same return tuple as `_run_processor_with_model_error`. When `raises` is
    given the call must raise it and `result` is None; the mocks are still
    returned so the error path's side effects can be asserted.
    """
    from ai_api.streams import processor as proc_module

    mock_db = MagicMock()
    mock_redis = AsyncMock()

    @asynccontextmanager
    async def mock_get_redis_client():
        yield mock_redis

    mock_whatsapp_client = AsyncMock()

    # Return None for the embedding service so the optional embedding step is skipped.
    # This keeps the test focused on error-path behavior.
    mock_embed_factory = MagicMock(return_value=None)

    mock_assistant_msg = MagicMock()
    mock_assistant_msg.id = uuid.uuid4()

    with (
        patch.object(proc_module, "SessionLocal", return_value=mock_db),
        patch.object(proc_module, "get_conversation_history", return_value=[]),
        patch.object(proc_module, "create_embedding_service", mock_embed_factory),
        patch.object(proc_module, "create_whatsapp_client", return_value=mock_whatsapp_client),
        patch.object(proc_module, "get_ai_response", agent_fn),
        patch.object(proc_module, "get_redis_client", mock_get_redis_client),
        patch.object(
            proc_module, "save_message", return_value=mock_assistant_msg
        ) as mock_save_message,
        patch.object(proc_module, "save_job_chunk", new_callable=AsyncMock) as mock_save_chunk,
        patch.object(proc_module, "set_job_metadata", new_callable=AsyncMock) as mock_set_meta,
    ):
        result = None
        with pytest.raises(raises) if raises else nullcontext():
            result = await proc_module.process_chat_job_direct(
                user_id=str(uuid.uuid4()),
                whatsapp_jid="5511999999999@s.whatsapp.net",
                message="Hi",
                conversation_type="private",
                user_message_id=str(uuid.uuid4()),
                job_id="job-test-001",
                whatsapp_message_id=whatsapp_message_id,
            )

    return result, mock_save_chunk, mock_set_meta, mock_save_message, mock_whatsapp_client


class TestModelErrorFallback:
    @pytest.mark.asyncio
    async def test_model_http_error_delivers_fallback_text_fallback(self):
        """Gemini 503 during streaming -> fallback chunk written, job marked complete, no raise."""
        exc = ModelHTTPError(
            status_code=503,
            model_name="gemini-3.1-flash-lite",
            body={
                "error": {
                    "code": 503,
                    "message": "This model is currently experiencing high demand.",
                    "status": "UNAVAILABLE",
                }
            },
        )

        (
            result,
            mock_save_chunk,
            mock_set_meta,
            mock_save_message,
            mock_whatsapp_client,
        ) = await _run_processor_with_model_error(exc)

        # Processor returned normally instead of raising
        assert result["success"] is True
        assert result["job_id"] == "job-test-001"
        assert result["db_message_id"] is None
        assert result["model_error"] is True

        # fallback text written as the single chunk (index 0)
        mock_save_chunk.assert_awaited_once()
        chunk_args = mock_save_chunk.call_args.args
        assert chunk_args[2] == 0
        assert chunk_args[3] == FALLBACK_TEXT

        # Job metadata set so /chat/job/{id} reports status=complete,
        # with db_message_id=None because we skip persisting the fallback.
        mock_set_meta.assert_awaited_once()
        meta_payload = mock_set_meta.call_args.args[2]
        assert meta_payload["db_message_id"] is None
        assert meta_payload["total_chunks"] == 1

        # Fallback must NOT be persisted to conversation history — otherwise
        # the next turn's message_history would echo the error text back to the LLM.
        mock_save_message.assert_not_called()

        # ❌ reaction placed on the user's original message
        mock_whatsapp_client.send_reaction.assert_awaited_once()
        reaction_args = mock_whatsapp_client.send_reaction.call_args.args
        assert reaction_args[1] == "wamid-test"
        assert reaction_args[2] == "❌"

    @pytest.mark.asyncio
    async def test_unexpected_model_behavior_also_falls_back_to_fallback_text(self):
        """Pydantic-AI UnexpectedModelBehavior is caught by the same handler."""
        exc = UnexpectedModelBehavior("Malformed tool call")

        (
            result,
            mock_save_chunk,
            mock_set_meta,
            mock_save_message,
            _mock_whatsapp_client,
        ) = await _run_processor_with_model_error(exc)

        assert result["success"] is True
        assert result["model_error"] is True
        mock_save_chunk.assert_awaited_once()
        assert mock_save_chunk.call_args.args[3] == FALLBACK_TEXT
        mock_set_meta.assert_awaited_once()
        mock_save_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_exception_group_also_delivers_fallback_text(self):
        """Both providers failing via FallbackModel -> FallbackExceptionGroup -> fallback text."""
        group = FallbackExceptionGroup(
            "All models from FallbackModel failed",
            [
                ModelHTTPError(
                    status_code=503,
                    model_name="gemini-3.1-flash-lite",
                    body=None,
                ),
                ModelHTTPError(
                    status_code=500,
                    model_name="deepseek-flash",
                    body=None,
                ),
            ],
        )

        (
            result,
            mock_save_chunk,
            mock_set_meta,
            mock_save_message,
            mock_whatsapp_client,
        ) = await _run_processor_with_model_error(group)

        assert result["success"] is True
        assert result["model_error"] is True
        mock_save_chunk.assert_awaited_once()
        assert mock_save_chunk.call_args.args[3] == FALLBACK_TEXT
        mock_set_meta.assert_awaited_once()
        mock_save_message.assert_not_called()
        mock_whatsapp_client.send_reaction.assert_awaited_once()
        assert mock_whatsapp_client.send_reaction.call_args.args[2] == "❌"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.RemoteProtocolError("peer closed connection mid-stream"),
            openai.APIError("overloaded", request=MagicMock(), body=None),
        ],
        ids=["transport-drop", "sse-error-event"],
    )
    async def test_raw_stream_errors_deliver_fallback_text(self, exc):
        """A DeepSeek stream that fails after its first chunk can't fall back and
        surfaces raw SDK/transport errors — they must still get the fallback reply
        and ❌ instead of silence."""
        (
            result,
            mock_save_chunk,
            mock_set_meta,
            mock_save_message,
            mock_whatsapp_client,
        ) = await _run_processor_with_model_error(exc)

        assert result["success"] is True
        assert result["model_error"] is True
        assert mock_save_chunk.call_args.args[3] == FALLBACK_TEXT
        mock_set_meta.assert_awaited_once()
        mock_save_message.assert_not_called()
        assert mock_whatsapp_client.send_reaction.call_args.args[2] == "❌"

    @pytest.mark.asyncio
    async def test_model_error_without_message_id_skips_reaction(self):
        """whatsapp_message_id=None -> fallback text delivered, reaction skipped."""
        exc = ModelHTTPError(
            status_code=503,
            model_name="gemini-3.1-flash-lite",
            body=None,
        )

        (
            result,
            mock_save_chunk,
            mock_set_meta,
            mock_save_message,
            mock_whatsapp_client,
        ) = await _run_processor_with_model_error(exc, whatsapp_message_id=None)

        assert result["success"] is True
        assert result["model_error"] is True
        mock_save_chunk.assert_awaited_once()
        assert mock_save_chunk.call_args.args[3] == FALLBACK_TEXT
        mock_set_meta.assert_awaited_once()
        mock_save_message.assert_not_called()
        mock_whatsapp_client.send_reaction.assert_not_awaited()


class TestModelErrorJobState:
    """The fallback must reach the user exactly once: the job is left COMPLETE
    (not "failed"), because the TS clients answer a failed job with their own
    error text + ❌ — publishing the fallback AND failing the job would double both."""

    @pytest.mark.asyncio
    async def test_model_error_job_is_not_marked_failed(self):
        exc = ModelHTTPError(status_code=503, model_name="gemini-3.1-flash-lite", body=None)
        _result, _chunk, mock_set_meta, _save, _wa = await _run_processor_with_model_error(exc)

        meta_payload = mock_set_meta.call_args.args[2]
        assert "status" not in meta_payload
        assert meta_payload["model_error"] is True

    @pytest.mark.asyncio
    async def test_model_error_job_status_reads_complete(self):
        """get_stream_job_status maps the fallback's metadata to 'complete'."""
        from ai_api.routes.chat import get_stream_job_status

        exc = ModelHTTPError(status_code=503, model_name="gemini-3.1-flash-lite", body=None)
        _result, _chunk, mock_set_meta, _save, _wa = await _run_processor_with_model_error(exc)
        meta_payload = mock_set_meta.call_args.args[2]

        redis = AsyncMock()
        with patch("ai_api.routes.chat.get_job_metadata", AsyncMock(return_value=meta_payload)):
            assert await get_stream_job_status(redis, "job-test-001") == "complete"

    @pytest.mark.asyncio
    async def test_non_model_error_still_fails_the_job(self):
        """Anything that is not a model error keeps the generic path: terminal
        "failed" metadata (clients stop polling) and the exception re-raised."""

        (
            result,
            mock_save_chunk,
            mock_set_meta,
            _mock_save_message,
            mock_whatsapp_client,
        ) = await _run_processor(_make_failing_agent(RuntimeError("boom")), raises=RuntimeError)

        assert result is None
        mock_save_chunk.assert_not_awaited()
        assert mock_set_meta.call_args.args[2]["status"] == "failed"
        mock_whatsapp_client.send_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_path_saves_reply(self):
        result, mock_save_chunk, mock_set_meta, mock_save_message, mock_wa = await _run_processor(
            _make_streaming_agent(["Hello", " there"])
        )

        assert result["success"] is True
        assert "model_error" not in result
        assert mock_save_chunk.call_args.args[3] == "Hello there"
        mock_save_message.assert_called_once()
        mock_wa.send_reaction.assert_not_awaited()
