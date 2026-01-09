"""Custom exceptions for WhatsApp client operations."""


class WhatsAppClientError(Exception):
    """Base exception for WhatsApp client errors."""

    def __init__(self, message: str, status_code: int | None = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class WhatsAppNotConnectedError(WhatsAppClientError):
    """Raised when WhatsApp is not connected."""

    def __init__(self, message: str = "WhatsApp is not connected"):
        super().__init__(message, status_code=503)


class WhatsAppNotFoundError(WhatsAppClientError):
    """Raised when phone number is not registered on WhatsApp."""

    def __init__(self, message: str = "Phone number not registered on WhatsApp"):
        super().__init__(message, status_code=404)
