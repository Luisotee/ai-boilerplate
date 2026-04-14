"""
Unit tests for ai_api.queue.schemas — Pydantic model validation.

Tests cover:
- ChunkData: required fields
- EnqueueResponse: defaults and Literal constraints
- JobStatusResponse: defaults and Literal constraints
- JobMetadata: required and optional fields
"""

import pytest
from pydantic import ValidationError

from ai_api.queue.schemas import (
    ChunkData,
    EnqueueResponse,
    JobMetadata,
    JobStatusResponse,
)

# ---------------------------------------------------------------------------
# ChunkData
# ---------------------------------------------------------------------------


class TestChunkData:
    def test_valid(self):
        chunk = ChunkData(
            index=0,
            content="Hello",
            timestamp="2025-01-15T10:30:00Z",
        )
        assert chunk.index == 0
        assert chunk.content == "Hello"
        assert chunk.timestamp == "2025-01-15T10:30:00Z"

    def test_missing_index(self):
        with pytest.raises(ValidationError):
            ChunkData(content="Hello", timestamp="2025-01-15T10:30:00Z")

    def test_missing_content(self):
        with pytest.raises(ValidationError):
            ChunkData(index=0, timestamp="2025-01-15T10:30:00Z")

    def test_missing_timestamp(self):
        with pytest.raises(ValidationError):
            ChunkData(index=0, content="Hello")

    def test_empty_content(self):
        chunk = ChunkData(index=0, content="", timestamp="2025-01-15T10:30:00Z")
        assert chunk.content == ""

    def test_large_index(self):
        chunk = ChunkData(index=9999, content="test", timestamp="2025-01-15T10:30:00Z")
        assert chunk.index == 9999

    def test_negative_index(self):
        # Pydantic int field does not restrict negatives by default
        chunk = ChunkData(index=-1, content="test", timestamp="2025-01-15T10:30:00Z")
        assert chunk.index == -1


# ---------------------------------------------------------------------------
# EnqueueResponse
# ---------------------------------------------------------------------------


class TestEnqueueResponse:
    def test_valid_with_defaults(self):
        resp = EnqueueResponse(job_id="job-123")
        assert resp.job_id == "job-123"
        assert resp.status == "queued"
        assert resp.message == "Job queued successfully"

    def test_missing_job_id(self):
        with pytest.raises(ValidationError):
            EnqueueResponse()

    def test_status_is_literal_queued(self):
        resp = EnqueueResponse(job_id="job-123")
        assert resp.status == "queued"

    def test_invalid_status(self):
        with pytest.raises(ValidationError):
            EnqueueResponse(job_id="job-123", status="processing")

    def test_custom_message(self):
        resp = EnqueueResponse(job_id="job-123", message="Custom message")
        assert resp.message == "Custom message"

    def test_default_message(self):
        resp = EnqueueResponse(job_id="job-123")
        assert resp.message == "Job queued successfully"


# ---------------------------------------------------------------------------
# JobStatusResponse
# ---------------------------------------------------------------------------


class TestJobStatusResponse:
    def test_minimal_valid(self):
        resp = JobStatusResponse(job_id="job-123", status="queued")
        assert resp.job_id == "job-123"
        assert resp.status == "queued"
        assert resp.chunks == []
        assert resp.total_chunks == 0
        assert resp.complete is False
        assert resp.full_response is None
        assert resp.error is None

    def test_status_queued(self):
        resp = JobStatusResponse(job_id="j1", status="queued")
        assert resp.status == "queued"

    def test_status_in_progress(self):
        resp = JobStatusResponse(job_id="j1", status="in_progress")
        assert resp.status == "in_progress"

    def test_status_complete(self):
        resp = JobStatusResponse(
            job_id="j1",
            status="complete",
            complete=True,
            full_response="All done",
        )
        assert resp.status == "complete"
        assert resp.complete is True
        assert resp.full_response == "All done"

    def test_status_failed(self):
        resp = JobStatusResponse(
            job_id="j1",
            status="failed",
            complete=True,
            error="Something went wrong",
        )
        assert resp.status == "failed"
        assert resp.error == "Something went wrong"

    def test_status_not_found(self):
        resp = JobStatusResponse(job_id="j1", status="not_found")
        assert resp.status == "not_found"

    def test_invalid_status(self):
        with pytest.raises(ValidationError):
            JobStatusResponse(job_id="j1", status="unknown")

    def test_missing_job_id(self):
        with pytest.raises(ValidationError):
            JobStatusResponse(status="queued")

    def test_missing_status(self):
        with pytest.raises(ValidationError):
            JobStatusResponse(job_id="j1")

    def test_with_chunks(self):
        chunk = ChunkData(index=0, content="word", timestamp="2025-01-15T10:30:00Z")
        resp = JobStatusResponse(
            job_id="j1",
            status="in_progress",
            chunks=[chunk],
            total_chunks=1,
        )
        assert len(resp.chunks) == 1
        assert resp.chunks[0].content == "word"
        assert resp.total_chunks == 1

    def test_defaults(self):
        resp = JobStatusResponse(job_id="j1", status="queued")
        assert resp.chunks == []
        assert resp.total_chunks == 0
        assert resp.complete is False
        assert resp.full_response is None
        assert resp.error is None


# ---------------------------------------------------------------------------
# JobMetadata
# ---------------------------------------------------------------------------


class TestJobMetadata:
    def test_minimal_valid(self):
        meta = JobMetadata(
            user_id="user-1",
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
        )
        assert meta.user_id == "user-1"
        assert meta.whatsapp_jid == "123@s.whatsapp.net"
        assert meta.message == "Hello"
        assert meta.conversation_type == "private"

    def test_defaults(self):
        meta = JobMetadata(
            user_id="user-1",
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
        )
        assert meta.total_chunks == 0
        assert meta.db_message_id is None
        assert meta.user_message_id is None
        assert meta.created_at is None

    def test_group_conversation_type(self):
        meta = JobMetadata(
            user_id="user-1",
            whatsapp_jid="123@g.us",
            message="Hello",
            conversation_type="group",
        )
        assert meta.conversation_type == "group"

    def test_invalid_conversation_type(self):
        with pytest.raises(ValidationError):
            JobMetadata(
                user_id="user-1",
                whatsapp_jid="123@s.whatsapp.net",
                message="Hello",
                conversation_type="broadcast",
            )

    def test_with_all_optional_fields(self):
        meta = JobMetadata(
            user_id="user-1",
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
            total_chunks=5,
            db_message_id="msg-uuid-1",
            user_message_id="msg-uuid-2",
            created_at="2025-01-15T10:30:00Z",
        )
        assert meta.total_chunks == 5
        assert meta.db_message_id == "msg-uuid-1"
        assert meta.user_message_id == "msg-uuid-2"
        assert meta.created_at == "2025-01-15T10:30:00Z"

    def test_missing_user_id(self):
        with pytest.raises(ValidationError):
            JobMetadata(
                whatsapp_jid="123@s.whatsapp.net",
                message="Hello",
                conversation_type="private",
            )

    def test_missing_whatsapp_jid(self):
        with pytest.raises(ValidationError):
            JobMetadata(
                user_id="user-1",
                message="Hello",
                conversation_type="private",
            )

    def test_missing_message(self):
        with pytest.raises(ValidationError):
            JobMetadata(
                user_id="user-1",
                whatsapp_jid="123@s.whatsapp.net",
                conversation_type="private",
            )

    def test_missing_conversation_type(self):
        with pytest.raises(ValidationError):
            JobMetadata(
                user_id="user-1",
                whatsapp_jid="123@s.whatsapp.net",
                message="Hello",
            )
