"""WhatsApp client module for AI API."""

from .client import SharedGroup, WhatsAppClient, create_whatsapp_client
from .exceptions import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)

__all__ = [
    "SharedGroup",
    "WhatsAppClient",
    "create_whatsapp_client",
    "WhatsAppClientError",
    "WhatsAppNotConnectedError",
    "WhatsAppNotFoundError",
]
