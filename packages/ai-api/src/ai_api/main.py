import asyncio
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings
from .database import init_db
from .deps import limiter
from .logger import logger
from .queue.connection import close_arq_redis, get_arq_redis
from .routes import (
    chat_router,
    health_router,
    knowledge_base_router,
    preferences_router,
    speech_router,
)
from .scripts.cleanup_expired_documents import cleanup_expired_documents


async def _cleanup_loop():
    """Periodically delete expired conversation-scoped documents."""
    interval = settings.cleanup_interval_minutes * 60
    while True:
        try:
            await cleanup_expired_documents()
        except Exception:
            logger.error("Expired document cleanup failed", exc_info=True)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database and Redis on startup"""
    logger.info("Starting AI API service...")

    # Initialize PostgreSQL
    init_db()

    # Initialize Redis connection pool
    try:
        await get_arq_redis()  # Initialize connection pool
        logger.info("✅ Redis connection pool initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Redis: {e}")
        raise

    # Start periodic expired-document cleanup
    cleanup_task = asyncio.create_task(_cleanup_loop())

    logger.info("=" * 60)
    logger.info("AI API is ready!")
    logger.info("=" * 60)
    logger.info("📚 API Documentation:")
    logger.info("   Swagger UI: http://localhost:8000/docs")
    logger.info("   ReDoc:      http://localhost:8000/redoc")
    logger.info("   OpenAPI:    http://localhost:8000/openapi.json")
    logger.info("=" * 60)
    logger.info("🏥 Health Check: http://localhost:8000/health")
    logger.info("=" * 60)

    yield

    # Cleanup on shutdown
    logger.info("Shutting down AI API service...")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    await close_arq_redis()
    logger.info("✅ Redis connection pool closed")


app = FastAPI(
    title="AI WhatsApp Agent API",
    version="1.0.0",
    description="""
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
    """,
    lifespan=lifespan,
    swagger_ui_parameters={"persistAuthorization": True},
)


def custom_openapi():
    """Add API key security scheme to generated OpenAPI spec."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=[
            {"name": "Health", "description": "Health check endpoints"},
            {
                "name": "Knowledge Base",
                "description": "PDF upload, processing, and semantic search",
            },
            {"name": "Chat", "description": "Synchronous and async chat with the AI agent"},
            {"name": "Speech-to-Text", "description": "Audio transcription via Whisper"},
            {"name": "Text-to-Speech", "description": "Speech synthesis via Gemini TTS"},
            {"name": "Preferences", "description": "Per-user conversation preferences"},
        ],
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "API key for authentication",
        }
    }
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]

# --- Security Middleware ---

_AUTH_EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json")


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validate X-API-Key header on all requests except health/docs."""

    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            return await call_next(request)

        api_key = request.headers.get("x-api-key")
        if not api_key or not hmac.compare_digest(api_key, settings.ai_api_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)


app.add_middleware(APIKeyMiddleware)

# CORS — added after APIKeyMiddleware so it runs first (Starlette LIFO order)
# This ensures preflight OPTIONS requests are handled before auth rejects them
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (slowapi with Redis backend)
app.state.limiter = limiter
app.add_exception_handler(
    RateLimitExceeded,
    lambda req, exc: JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    ),
)
app.add_middleware(SlowAPIMiddleware)

# --- Register Routers ---

app.include_router(health_router)
app.include_router(knowledge_base_router)
app.include_router(chat_router)
app.include_router(speech_router)
app.include_router(preferences_router)
