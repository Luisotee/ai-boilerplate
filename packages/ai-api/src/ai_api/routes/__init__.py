"""Route modules for the AI API.

Each module provides an APIRouter instance with related endpoints.
"""

from .chat import router as chat_router
from .health import router as health_router
from .knowledge_base import router as knowledge_base_router
from .preferences import router as preferences_router
from .speech import router as speech_router

__all__ = [
    "chat_router",
    "health_router",
    "knowledge_base_router",
    "preferences_router",
    "speech_router",
]
