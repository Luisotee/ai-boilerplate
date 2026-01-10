from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    whatsapp_jid: str = Field(..., description="User's WhatsApp JID")
    message: str = Field(..., description="User's message text")
    conversation_type: Literal["private", "group"] = Field(
        ..., description="Conversation type (private or group)"
    )
    sender_jid: str | None = Field(None, description="Sender JID in group chats")
    sender_name: str | None = Field(None, description="Sender name in group chats")
    whatsapp_message_id: str | None = Field(None, description="WhatsApp message ID for reactions")
    image_data: str | None = Field(None, description="Base64-encoded image data for vision")
    image_mimetype: str | None = Field(None, description="Image MIME type (e.g., image/jpeg)")
    document_data: str | None = Field(None, description="Base64-encoded PDF document data")
    document_mimetype: str | None = Field(
        None, description="Document MIME type (e.g., application/pdf)"
    )
    document_filename: str | None = Field(None, description="Original document filename")


class ChatResponse(BaseModel):
    response: str


class SaveMessageRequest(BaseModel):
    """Request to save message without generating AI response"""

    whatsapp_jid: str = Field(..., description="User's WhatsApp JID")
    message: str = Field(..., description="Message text to save")
    conversation_type: Literal["private", "group"] = Field(
        ..., description="Conversation type (private or group)"
    )
    sender_jid: str | None = Field(None, description="Sender JID in group chats")
    sender_name: str | None = Field(None, description="Sender name in group chats")
    whatsapp_message_id: str | None = Field(None, description="WhatsApp message ID for reactions")


class UploadPDFResponse(BaseModel):
    """Response after uploading a PDF to the knowledge base"""

    document_id: str = Field(..., description="UUID of the uploaded document")
    filename: str = Field(..., description="Original filename of the uploaded PDF")
    status: str = Field(
        ..., description="Processing status (pending, processing, completed, failed)"
    )
    message: str = Field(..., description="Human-readable status message")


class FileUploadResult(BaseModel):
    """Result for a single file in a batch upload"""

    filename: str = Field(..., description="Original filename")
    status: Literal["accepted", "rejected"] = Field(..., description="Upload status")
    document_id: str | None = Field(None, description="UUID if accepted")
    message: str | None = Field(None, description="Success message")
    error: str | None = Field(None, description="Error message if rejected")


class BatchUploadResponse(BaseModel):
    """Response for batch PDF upload"""

    total_files: int = Field(..., description="Total number of files in batch")
    accepted: int = Field(..., description="Number of files accepted for processing")
    rejected: int = Field(..., description="Number of files rejected")
    results: list[FileUploadResult] = Field(..., description="Per-file results")
    message: str = Field(..., description="Overall batch status message")


class TranscribeResponse(BaseModel):
    """Audio transcription response - TEXT ONLY"""

    transcription: str = Field(..., description="Transcribed text from audio")
    message: str = Field(..., description="Status message")
    # NOTE: No ai_response field - client will call /chat/enqueue separately


class TTSRequest(BaseModel):
    """Text-to-speech synthesis request"""

    text: str = Field(..., description="Text to convert to speech")
    whatsapp_jid: str | None = Field(
        None, description="Optional JID to fetch user language preferences"
    )
    format: Literal["ogg", "mp3", "wav", "flac"] = Field(
        "ogg", description="Output audio format (default: ogg for WhatsApp compatibility)"
    )


class CommandResponse(BaseModel):
    """Response for command execution (e.g., /settings, /tts on)"""

    is_command: bool = Field(True, description="Always true for command responses")
    response: str = Field(..., description="Command result message")


class PreferencesResponse(BaseModel):
    """User preferences"""

    tts_enabled: bool = Field(..., description="Whether TTS is enabled")
    tts_language: str = Field(..., description="TTS language code (e.g., 'en', 'es')")
    stt_language: str | None = Field(None, description="STT language code, null for auto-detect")


class UpdatePreferencesRequest(BaseModel):
    """Request to update preferences"""

    tts_enabled: bool | None = Field(None, description="Enable/disable TTS")
    tts_language: str | None = Field(None, description="TTS language code")
    stt_language: str | None = Field(None, description="STT language code, 'auto' converts to null")
