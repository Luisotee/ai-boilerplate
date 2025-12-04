import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from dotenv import load_dotenv

# Load environment variables BEFORE importing local modules
load_dotenv()

from .logger import logger
from .database import init_db, get_db, get_conversation_history, save_message
from .schemas import ChatRequest, ChatResponse, SaveMessageRequest
from .agent import get_ai_response, format_message_history

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup"""
    logger.info('Starting AI API service...')
    init_db()
    logger.info('=' * 60)
    logger.info('AI API is ready!')
    logger.info('=' * 60)
    logger.info('📚 API Documentation:')
    logger.info('   Swagger UI: http://localhost:8000/docs')
    logger.info('   ReDoc:      http://localhost:8000/redoc')
    logger.info('   OpenAPI:    http://localhost:8000/openapi.json')
    logger.info('=' * 60)
    logger.info('🏥 Health Check: http://localhost:8000/health')
    logger.info('=' * 60)
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

@app.post('/chat/save', tags=['Chat'])
async def save_message_only(request: SaveMessageRequest, db: Session = Depends(get_db)):
    """
    Save a message without generating AI response

    Used for group messages where bot shouldn't respond but needs to maintain context.

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (group or private)
    - `message`: Message text
    - `sender_jid`: Optional sender JID (for group messages)
    - `sender_name`: Optional sender name (for group messages)

    **Response:**
    - `success`: Boolean indicating if save was successful
    """
    logger.info(f'Saving message from {request.whatsapp_jid} (no response)')

    try:
        # Format message with sender name if group message
        content = f"{request.sender_name}: {request.message}" if request.sender_name else request.message

        # Save user message only
        save_message(
            db,
            request.whatsapp_jid,
            'user',
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name
        )

        return {'success': True}

    except Exception as e:
        logger.error(f'Error saving message: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')

@app.post('/chat/stream', tags=['Chat'])
async def chat_stream(request: ChatRequest, db: Session = Depends(get_db)):
    """
    Stream AI response for a chat message

    This endpoint accepts a message and streams the AI response using Server-Sent Events (SSE).

    **Request Body:**
    - `whatsapp_jid`: WhatsApp JID (e.g., "70253400879283@lid" or "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - Server-Sent Events stream with AI response chunks
    - Format: `data: <content>\\n\\n`
    - End signal: `data: [DONE]\\n\\n`
    """
    logger.info(f'Received chat request from {request.whatsapp_jid}')

    try:
        # Get conversation history with type-specific limit
        history = get_conversation_history(
            db,
            request.whatsapp_jid,
            request.conversation_type
        )
        message_history = format_message_history(history) if history else None

        # Format message with sender name if provided (group message)
        content = f"{request.sender_name}: {request.message}" if request.sender_name else request.message

        # Save user message with group context
        save_message(
            db,
            request.whatsapp_jid,
            'user',
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name
        )

        # Stream response
        async def generate():
            full_response = ""
            try:
                # Stream tokens from AI as they arrive
                async for token in get_ai_response(content, message_history):
                    full_response += token
                    yield f'data: {token}\n\n'

                # Save complete assistant response after streaming completes
                save_message(
                    db,
                    request.whatsapp_jid,
                    'assistant',
                    full_response,
                    request.conversation_type
                )

                yield 'data: [DONE]\n\n'

            except Exception as e:
                logger.error(f'Error streaming response: {str(e)}', exc_info=True)

                # Save partial response if any
                if full_response:
                    save_message(
                        db,
                        request.whatsapp_jid,
                        'assistant',
                        full_response,
                        request.conversation_type
                    )

                yield 'data: [ERROR]\n\n'

        return StreamingResponse(
            generate(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no'  # Disable nginx buffering
            }
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
    - `whatsapp_jid`: WhatsApp JID (e.g., "70253400879283@lid" or "1234567890@s.whatsapp.net")
    - `message`: User's message text

    **Response:**
    - `response`: Complete AI-generated response text
    """
    logger.info(f'Received chat request from {request.whatsapp_jid}')

    try:
        # Get conversation history with type-specific limit
        history = get_conversation_history(
            db,
            request.whatsapp_jid,
            request.conversation_type
        )
        message_history = format_message_history(history) if history else None

        # Format message with sender name if provided (group message)
        content = f"{request.sender_name}: {request.message}" if request.sender_name else request.message

        # Save user message with group context
        save_message(
            db,
            request.whatsapp_jid,
            'user',
            content,
            request.conversation_type,
            sender_jid=request.sender_jid,
            sender_name=request.sender_name
        )

        # Get AI response (using formatted content) - consume stream into complete response
        ai_response = ""
        async for token in get_ai_response(content, message_history):
            ai_response += token

        # Save assistant response (no sender info for bot)
        save_message(
            db,
            request.whatsapp_jid,
            'assistant',
            ai_response,
            request.conversation_type
        )

        return ChatResponse(response=ai_response)

    except Exception as e:
        logger.error(f'Error processing chat: {str(e)}', exc_info=True)
        raise HTTPException(status_code=500, detail='Internal server error')
