from pydantic import BaseModel, Field
from typing import Literal, Optional, List

class ChatRequest(BaseModel):
    whatsapp_jid: str = Field(..., description="User's WhatsApp JID")
    message: str = Field(..., description="User's message text")
    conversation_type: Literal['private', 'group'] = Field(..., description="Conversation type (private or group)")
    sender_jid: str | None = Field(None, description="Sender JID in group chats")
    sender_name: str | None = Field(None, description="Sender name in group chats")

class ChatResponse(BaseModel):
    response: str

class SaveMessageRequest(BaseModel):
    """Request to save message without generating AI response"""
    whatsapp_jid: str = Field(..., description="User's WhatsApp JID")
    message: str = Field(..., description="Message text to save")
    conversation_type: Literal['private', 'group'] = Field(..., description="Conversation type (private or group)")
    sender_jid: str | None = Field(None, description="Sender JID in group chats")
    sender_name: str | None = Field(None, description="Sender name in group chats")

class UploadPDFResponse(BaseModel):
    """Response after uploading a PDF to the knowledge base"""
    document_id: str = Field(..., description="UUID of the uploaded document")
    filename: str = Field(..., description="Original filename of the uploaded PDF")
    status: str = Field(..., description="Processing status (pending, processing, completed, failed)")
    message: str = Field(..., description="Human-readable status message")

class FileUploadResult(BaseModel):
    """Result for a single file in a batch upload"""
    filename: str = Field(..., description="Original filename")
    status: Literal['accepted', 'rejected'] = Field(..., description="Upload status")
    document_id: Optional[str] = Field(None, description="UUID if accepted")
    message: Optional[str] = Field(None, description="Success message")
    error: Optional[str] = Field(None, description="Error message if rejected")

class BatchUploadResponse(BaseModel):
    """Response for batch PDF upload"""
    total_files: int = Field(..., description="Total number of files in batch")
    accepted: int = Field(..., description="Number of files accepted for processing")
    rejected: int = Field(..., description="Number of files rejected")
    results: List[FileUploadResult] = Field(..., description="Per-file results")
    message: str = Field(..., description="Overall batch status message")

class TranscribeResponse(BaseModel):
    """Audio transcription response - TEXT ONLY"""
    transcription: str = Field(..., description="Transcribed text from audio")
    message: str = Field(..., description="Status message")
    # NOTE: No ai_response field - client will call /chat/enqueue separately
