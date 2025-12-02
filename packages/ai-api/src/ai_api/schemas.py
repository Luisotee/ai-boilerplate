from pydantic import BaseModel

class ChatRequest(BaseModel):
    whatsapp_jid: str
    message: str
    sender_jid: str | None = None  # Participant JID for group messages
    sender_name: str | None = None  # Participant name for group messages

class ChatResponse(BaseModel):
    response: str

class SaveMessageRequest(BaseModel):
    """Request to save message without generating AI response"""
    whatsapp_jid: str
    message: str
    sender_jid: str | None = None
    sender_name: str | None = None
