from pydantic import BaseModel, Field
from typing import Literal

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
