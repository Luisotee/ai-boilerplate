"""
Pydantic schemas for queue-related API requests and responses.
"""

from typing import Literal

from pydantic import BaseModel, Field


class ChunkData(BaseModel):
    """
    Individual streaming chunk data.
    """

    index: int = Field(..., description="Sequential chunk index (0-based)")
    content: str = Field(..., description="Chunk content (token or text fragment)")
    timestamp: str = Field(..., description="ISO format timestamp when chunk was created")


class EnqueueResponse(BaseModel):
    """
    Response when a job is successfully enqueued.
    """

    job_id: str = Field(..., description="Unique job identifier for tracking")
    status: Literal["queued"] = Field(default="queued", description="Initial job status")
    message: str = Field(default="Job queued successfully", description="Human-readable message")


class JobMedia(BaseModel):
    """
    One image to send after the reply text. Fetch its bytes from
    `GET /chat/job/{job_id}/media/{index}`.
    """

    index: int = Field(..., description="Position, used in the media URL")
    caption: str = Field(default="", description="Caption (max 1024 chars), may be empty")
    mimetype: str = Field(..., description="image/jpeg or image/png")
    url: str = Field(..., description="Source URL, for a text fallback if sending fails")


class JobStatusResponse(BaseModel):
    """
    Response for job status polling.
    """

    job_id: str = Field(..., description="Unique job identifier")
    status: Literal["queued", "in_progress", "complete", "failed", "not_found"] = Field(
        ..., description="Current job status"
    )
    chunks: list[ChunkData] = Field(
        default_factory=list, description="Accumulated streaming chunks"
    )
    total_chunks: int = Field(default=0, description="Total number of chunks available")
    complete: bool = Field(
        default=False, description="Whether job has finished (success or failure)"
    )
    full_response: str | None = Field(
        default=None,
        description="Complete assembled response (only present when status=complete)",
    )
    error: str | None = Field(default=None, description="Error message if status=failed")
    media: list[JobMedia] | None = Field(
        default=None,
        description="Images to send after the text (only present when status=complete)",
    )


class JobMetadata(BaseModel):
    """
    Internal job metadata stored in Redis.
    """

    user_id: str
    whatsapp_jid: str
    message: str
    conversation_type: Literal["private", "group"]
    total_chunks: int = 0
    db_message_id: str | None = None  # UUID of saved assistant message
    user_message_id: str | None = None  # UUID of saved user message
    created_at: str | None = None  # ISO timestamp
