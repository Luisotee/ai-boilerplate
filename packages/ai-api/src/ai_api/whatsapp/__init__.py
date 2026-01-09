"""WhatsApp client module for AI API."""

from .client import WhatsAppClient, create_whatsapp_client
from .exceptions import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)

__all__ = [
    "WhatsAppClient",
    "create_whatsapp_client",
    "WhatsAppClientError",
    "WhatsAppNotConnectedError",
    "WhatsAppNotFoundError",
]
