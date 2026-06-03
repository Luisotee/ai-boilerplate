from datetime import datetime
from typing import Any, Literal

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
    is_group_admin: bool | None = Field(
        None, description="Whether the sender is a group admin (groups only)"
    )
    phone: str | None = Field(None, description="E.164 phone number (e.g., +5491126726818)")
    whatsapp_lid: str | None = Field(None, description="WhatsApp LID if known")
    client_id: Literal["baileys", "cloud", "telegram"] | None = Field(
        None,
        description="Chat client identifier for routing callbacks",
    )


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
    phone: str | None = Field(None, description="E.164 phone number (e.g., +5491126726818)")
    whatsapp_lid: str | None = Field(None, description="WhatsApp LID if known")


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


# --- Admin (management dashboard) ---


class PromptResponse(BaseModel):
    """Active system prompt for this bot."""

    content: str = Field(..., description="Effective prompt (override if set, else the default)")
    is_overridden: bool = Field(..., description="True if a DB override is active")
    default_length: int = Field(..., description="Character length of the hardcoded default prompt")
    updated_at: datetime | None = Field(None, description="When the override was last saved")


class UpdatePromptRequest(BaseModel):
    """Request to set the active system-prompt override."""

    content: str = Field(
        ...,
        min_length=1,
        max_length=100_000,
        description="New system prompt (non-empty; capped at 100 KB)",
    )


class SettingItem(BaseModel):
    """A single configurable setting with its effective value and metadata."""

    key: str
    value: Any = Field(..., description="Effective value (override if set, else env default)")
    default: Any = Field(..., description="Env/code default value")
    source: Literal["default", "override"] = Field(..., description="Where the value comes from")
    hot: bool = Field(..., description="True if overridable at runtime; False = needs restart")
    category: str
    type: Literal["str", "int", "float", "bool"]
    description: str
    choices: list[str] | None = Field(None, description="Allowed values, if constrained")
    secret: bool = Field(False, description="True if the value is masked")


class SettingsResponse(BaseModel):
    """All registered settings."""

    settings: list[SettingItem]


class UpdateSettingsRequest(BaseModel):
    """Request to set one or more runtime-setting overrides."""

    overrides: dict[str, Any] = Field(..., description="Map of setting key → new value")


class UserSummary(BaseModel):
    """A conversation/user row for the dashboard's user list."""

    whatsapp_jid: str
    name: str | None = None
    conversation_type: str
    message_count: int
    last_message_at: datetime | None = None


class UsersResponse(BaseModel):
    """Paginated list of users."""

    users: list[UserSummary]
    total: int
    limit: int
    offset: int


class MessageItem(BaseModel):
    """A single conversation message (read-only view)."""

    role: str
    content: str
    sender_name: str | None = None
    timestamp: datetime


class MessagesResponse(BaseModel):
    """Paginated conversation history for one user (newest first)."""

    whatsapp_jid: str
    messages: list[MessageItem]
    total: int
    limit: int
    offset: int


class OverviewResponse(BaseModel):
    """High-level counts for the dashboard landing page."""

    users: int
    messages: int
    knowledge_base_documents: int


class WhatsAppStatusResponse(BaseModel):
    """Baileys WhatsApp link status + pairing QR (proxied from the client)."""

    status: Literal["connecting", "qr", "connected", "disconnected", "unavailable"] = Field(
        ..., description="Link lifecycle; 'unavailable' = no Baileys client reachable"
    )
    connected: bool = Field(..., description="True when the session is linked")
    qr: str | None = Field(None, description="Raw pairing QR payload; present only while 'qr'")
    qr_generated_at: str | None = Field(None, description="ISO timestamp the QR was issued")
