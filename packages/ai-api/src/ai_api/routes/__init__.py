"""Route modules for the AI API.

Each module provides an APIRouter instance with related endpoints.
"""

from .access import router as access_router
from .admin import router as admin_router
from .broadcasts import router as broadcasts_router
from .bug_reports import router as bug_reports_router
from .chat import router as chat_router
from .contact import router as contact_router
from .health import router as health_router
from .knowledge_base import router as knowledge_base_router
from .message_events import router as message_events_router
from .preferences import router as preferences_router
from .speech import router as speech_router

__all__ = [
    "access_router",
    "admin_router",
    "broadcasts_router",
    "bug_reports_router",
    "chat_router",
    "contact_router",
    "health_router",
    "knowledge_base_router",
    "message_events_router",
    "preferences_router",
    "speech_router",
]
