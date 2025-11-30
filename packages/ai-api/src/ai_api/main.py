import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from .logger import logger
from .database import init_db, get_db, get_conversation_history, save_message
from .schemas import ChatRequest, ChatResponse
from .agent import get_ai_response, format_message_history

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup"""
    logger.info('Starting AI API service...')
    init_db()
    yield
    logger.info('Shutting down AI API service...')

app = FastAPI(
    title='AI WhatsApp Agent API',
    version='1.0.0',
    description='''
    ## AI WhatsApp Agent API

    A FastAPI service that powers an AI chatbot with conversation memory.

    ### Features
    - 🤖 **AI-powered responses** using Google Gemini via Pydantic AI
    - 💬 **Conversation memory** stored in PostgreSQL
    - 📡 **Streaming support** via Server-Sent Events (SSE)
    - 🔄 **Cross-platform** - works with WhatsApp, Telegram, and more

    ### Endpoints
    - `/health` - Health check endpoint
    - `/chat` - Non-streaming chat endpoint
    - `/chat/stream` - Streaming chat endpoint (SSE)

    ### Auto-Generated Documentation
    - **Swagger UI**: Available at `/docs`
    - **ReDoc**: Available at `/redoc`
    - **OpenAPI Schema**: Available at `/openapi.json`
    ''',
    lifespan=lifespan
)

@app.get('/health', tags=['Health'])
async def health_check():
    """
    Health check endpoint

    Returns the service health status.
    """
    return {'status': 'healthy'}

@app.post('/chat/stream', tags=['Chat'])
async def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Stream AI response for a chat message

    This endpoint accepts a message and streams the AI response using Server-Sent Events (SSE).

    **Request Body:**
    - `phone`: User's phone number (e.g., "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - Server-Sent Events stream with AI response chunks
    - Format: `data: <content>\\n\\n`
    - End signal: `data: [DONE]\\n\\n`
    """
    logger.info(f'Received chat request from {request.phone}')

    try:
        # Get conversation history
        history = get_conversation_history(db, request.phone, limit=10)
        message_history = format_message_history(history) if history else None

        # Save user message
        save_message(db, request.phone, 'user', request.message)

        # Get AI response
        ai_response = await get_ai_response(request.message, message_history)

        # Save assistant response
        save_message(db, request.phone, 'assistant', ai_response)

        # Stream response
        async def generate():
            # For MVP, send complete response in one chunk
            # Future: implement actual streaming with agent.run_stream()
            yield f'data: {ai_response}\n\n'
            yield 'data: [DONE]\n\n'

        return StreamingResponse(
            generate(),
            media_type='text/event-stream'
        )

    except Exception as e:
        logger.error(f'Error processing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.post('/chat', response_model=ChatResponse, tags=['Chat'])
async def chat(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Non-streaming chat endpoint

    Alternative to `/chat/stream` that returns the complete response in a single JSON payload.

    **Request Body:**
    - `phone`: User's phone number (e.g., "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - `response`: Complete AI-generated response text
    """
    logger.info(f'Received chat request from {request.phone}')

    try:
        # Get conversation history
        history = get_conversation_history(db, request.phone, limit=10)
        message_history = format_message_history(history) if history else None

        # Save user message
        save_message(db, request.phone, 'user', request.message)

        # Get AI response
        ai_response = await get_ai_response(request.message, message_history)

        # Save assistant response
        save_message(db, request.phone, 'assistant', ai_response)

        return ChatResponse(response=ai_response)

    except Exception as e:
        logger.error(f'Error processing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')
