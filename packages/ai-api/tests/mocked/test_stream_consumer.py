"""Tests for the stream consumer message processing."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ai_api.streams.consumer import process_single_message


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_data(overrides=None):
    """
    Build a valid Redis stream message data dict (bytes keys/values).

    All required fields are populated with defaults; pass overrides to
    customize specific fields.
    """
    base = {
        b"job_id": b"job-001",
        b"user_id": b"user-123",
        b"whatsapp_jid": b"5511999999999@s.whatsapp.net",
        b"message": b"Hello from user",
        b"conversation_type": b"private",
        b"user_message_id": b"umid-001",
    }
    if overrides:
        base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


class TestMissingFields:
    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_job_id_raises_value_error(self, mock_processor):
        data = _make_stream_data()
        del data[b"job_id"]

        with pytest.raises(ValueError, match="Missing required fields.*job_id"):
            await process_single_message("user-123", "stream-msg-1", data)

        mock_processor.assert_not_called()

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_user_id_raises_value_error(self, mock_processor):
        data = _make_stream_data()
        del data[b"user_id"]

        with pytest.raises(ValueError, match="Missing required fields.*user_id"):
            await process_single_message("user-123", "stream-msg-1", data)

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_message_raises_value_error(self, mock_processor):
        data = _make_stream_data()
        del data[b"message"]

        with pytest.raises(ValueError, match="Missing required fields.*message"):
            await process_single_message("user-123", "stream-msg-1", data)

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_whatsapp_jid_raises_value_error(self, mock_processor):
        data = _make_stream_data()
        del data[b"whatsapp_jid"]

        with pytest.raises(ValueError, match="Missing required fields.*whatsapp_jid"):
            await process_single_message("user-123", "stream-msg-1", data)

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_conversation_type_raises_value_error(self, mock_processor):
        data = _make_stream_data()
        del data[b"conversation_type"]

        with pytest.raises(ValueError, match="Missing required fields.*conversation_type"):
            await process_single_message("user-123", "stream-msg-1", data)

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_user_message_id_raises_value_error(self, mock_processor):
        data = _make_stream_data()
        del data[b"user_message_id"]

        with pytest.raises(ValueError, match="Missing required fields.*user_message_id"):
            await process_single_message("user-123", "stream-msg-1", data)

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_multiple_fields_lists_all(self, mock_processor):
        data = _make_stream_data()
        del data[b"job_id"]
        del data[b"message"]

        with pytest.raises(ValueError, match="Missing required fields"):
            await process_single_message("user-123", "stream-msg-1", data)


# ---------------------------------------------------------------------------
# safe_decode utility (tested indirectly through process_single_message)
# ---------------------------------------------------------------------------


class TestSafeDecode:
    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_bytes_values_are_decoded(self, mock_processor):
        data = _make_stream_data()
        await process_single_message("user-123", "stream-msg-1", data)

        mock_processor.assert_called_once()
        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["job_id"] == "job-001"
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["message"] == "Hello from user"

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_optional_fields_default_to_none(self, mock_processor):
        data = _make_stream_data()
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["whatsapp_message_id"] is None
        assert call_kwargs["sender_name"] is None
        assert call_kwargs["client_id"] is None

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_optional_sender_name_decoded(self, mock_processor):
        data = _make_stream_data({b"sender_name": b"John Doe"})
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["sender_name"] == "John Doe"

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_optional_client_id_decoded(self, mock_processor):
        data = _make_stream_data({b"client_id": b"cloud"})
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["client_id"] == "cloud"


# ---------------------------------------------------------------------------
# has_image boolean parsing from Redis
# ---------------------------------------------------------------------------


class TestHasImageParsing:
    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_has_image_true_string(self, mock_processor):
        data = _make_stream_data(
            {
                b"has_image": b"true",
                b"image_mimetype": b"image/jpeg",
            }
        )
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["has_image"] is True
        assert call_kwargs["image_mimetype"] == "image/jpeg"

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_has_image_false_string(self, mock_processor):
        data = _make_stream_data({b"has_image": b"false"})
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["has_image"] is False
        assert call_kwargs["image_mimetype"] is None

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_missing_has_image_defaults_to_false(self, mock_processor):
        data = _make_stream_data()
        # has_image not in data at all — defaults to "false" via default param
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["has_image"] is False
        assert call_kwargs["image_mimetype"] is None

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_has_image_empty_bytes_defaults_to_false(self, mock_processor):
        data = _make_stream_data({b"has_image": b""})
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["has_image"] is False


# ---------------------------------------------------------------------------
# has_document parsing
# ---------------------------------------------------------------------------


class TestHasDocumentParsing:
    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_has_document_true_with_fields(self, mock_processor):
        data = _make_stream_data(
            {
                b"has_document": b"true",
                b"document_id": b"doc-123",
                b"document_path": b"/tmp/doc.pdf",
                b"document_filename": b"report.pdf",
            }
        )
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["has_document"] is True
        assert call_kwargs["document_id"] == "doc-123"
        assert call_kwargs["document_path"] == "/tmp/doc.pdf"
        assert call_kwargs["document_filename"] == "report.pdf"

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_has_document_false_nullifies_fields(self, mock_processor):
        data = _make_stream_data({b"has_document": b"false"})
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["has_document"] is False
        assert call_kwargs["document_id"] is None
        assert call_kwargs["document_path"] is None
        assert call_kwargs["document_filename"] is None


# ---------------------------------------------------------------------------
# Full successful processing
# ---------------------------------------------------------------------------


class TestSuccessfulProcessing:
    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_calls_process_chat_job_direct(self, mock_processor):
        data = _make_stream_data()
        await process_single_message("user-123", "stream-msg-1", data)

        mock_processor.assert_called_once()

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_passes_all_required_fields(self, mock_processor):
        data = _make_stream_data()
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["user_id"] == "user-123"
        assert call_kwargs["whatsapp_jid"] == "5511999999999@s.whatsapp.net"
        assert call_kwargs["message"] == "Hello from user"
        assert call_kwargs["conversation_type"] == "private"
        assert call_kwargs["user_message_id"] == "umid-001"
        assert call_kwargs["job_id"] == "job-001"

    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_passes_whatsapp_message_id_when_present(self, mock_processor):
        data = _make_stream_data({b"whatsapp_message_id": b"wamid-456"})
        await process_single_message("user-123", "stream-msg-1", data)

        call_kwargs = mock_processor.call_args[1]
        assert call_kwargs["whatsapp_message_id"] == "wamid-456"


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    @pytest.mark.asyncio
    @patch("ai_api.streams.consumer.process_chat_job_direct", new_callable=AsyncMock)
    async def test_processor_exception_is_reraised(self, mock_processor):
        mock_processor.side_effect = RuntimeError("DB connection lost")
        data = _make_stream_data()

        with pytest.raises(RuntimeError, match="DB connection lost"):
            await process_single_message("user-123", "stream-msg-1", data)
