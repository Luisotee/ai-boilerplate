"""
Async HTTP client for WhatsApp REST API.

Provides type-safe methods for all WhatsApp messaging operations.
Uses httpx.AsyncClient passed via constructor for connection pooling.
"""

import logging
from dataclasses import dataclass

import httpx

from .exceptions import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)

logger = logging.getLogger(__name__)


@dataclass
class SendMessageResponse:
    """Response from send message operations."""

    success: bool
    message_id: str | None = None


@dataclass
class SuccessResponse:
    """Response from operations that only return success status."""

    success: bool


class WhatsAppClient:
    """Async HTTP client for WhatsApp REST API."""

    def __init__(self, http_client: httpx.AsyncClient, base_url: str):
        """
        Initialize WhatsApp client.

        Args:
            http_client: Shared httpx.AsyncClient for connection pooling
            base_url: WhatsApp client REST API base URL (e.g., http://localhost:3001)
        """
        self._client = http_client
        self._base_url = base_url.rstrip("/")

    async def _handle_response(self, response: httpx.Response) -> dict:
        """Handle HTTP response and raise appropriate errors."""
        if response.status_code == 503:
            raise WhatsAppNotConnectedError()

        if response.status_code == 404:
            error_data = response.json()
            raise WhatsAppNotFoundError(error_data.get("error", "Not found"))

        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_msg = error_data.get("error", "Unknown error")
            except Exception:
                error_msg = response.text or "Unknown error"
            raise WhatsAppClientError(error_msg, status_code=response.status_code)

        return response.json()

    async def send_text(
        self,
        phone_number: str,
        text: str,
        quoted_message_id: str | None = None,
    ) -> SendMessageResponse:
        """
        Send a text message.

        Args:
            phone_number: WhatsApp JID or phone number
            text: Message text content
            quoted_message_id: Optional message ID to quote/reply to

        Returns:
            SendMessageResponse with success status and message_id
        """
        payload: dict = {"phoneNumber": phone_number, "text": text}
        if quoted_message_id:
            payload["quoted_message_id"] = quoted_message_id

        logger.info(f"Sending text to {phone_number[:8]}...")

        response = await self._client.post(
            f"{self._base_url}/whatsapp/send-text",
            json=payload,
        )
        data = await self._handle_response(response)
        return SendMessageResponse(
            success=data.get("success", False),
            message_id=data.get("message_id"),
        )

    async def send_reaction(
        self,
        phone_number: str,
        message_id: str,
        emoji: str,
    ) -> SuccessResponse:
        """
        React to a message with emoji.

        Args:
            phone_number: WhatsApp JID or phone number
            message_id: ID of message to react to
            emoji: Emoji to react with (e.g., "👍", "❤️")

        Returns:
            SuccessResponse indicating operation result
        """
        logger.info(f"Sending reaction {emoji} to message {message_id[:8]}...")

        response = await self._client.post(
            f"{self._base_url}/whatsapp/send-reaction",
            json={
                "phoneNumber": phone_number,
                "message_id": message_id,
                "emoji": emoji,
            },
        )
        data = await self._handle_response(response)
        return SuccessResponse(success=data.get("success", False))

    async def send_location(
        self,
        phone_number: str,
        latitude: float,
        longitude: float,
        name: str | None = None,
        address: str | None = None,
    ) -> SendMessageResponse:
        """
        Send a location.

        Args:
            phone_number: WhatsApp JID or phone number
            latitude: Latitude coordinate (-90 to 90)
            longitude: Longitude coordinate (-180 to 180)
            name: Optional name for the location
            address: Optional address string

        Returns:
            SendMessageResponse with success status and message_id
        """
        payload: dict = {
            "phoneNumber": phone_number,
            "latitude": latitude,
            "longitude": longitude,
        }
        if name:
            payload["name"] = name
        if address:
            payload["address"] = address

        logger.info(f"Sending location ({latitude}, {longitude}) to {phone_number[:8]}...")

        response = await self._client.post(
            f"{self._base_url}/whatsapp/send-location",
            json=payload,
        )
        data = await self._handle_response(response)
        return SendMessageResponse(
            success=data.get("success", False),
            message_id=data.get("message_id"),
        )

    async def send_contact(
        self,
        phone_number: str,
        contact_name: str,
        contact_phone: str,
        contact_email: str | None = None,
        contact_org: str | None = None,
    ) -> SendMessageResponse:
        """
        Send a contact card (vCard).

        Args:
            phone_number: WhatsApp JID or phone number
            contact_name: Full name of the contact
            contact_phone: Phone number with country code
            contact_email: Optional email address
            contact_org: Optional organization name

        Returns:
            SendMessageResponse with success status and message_id
        """
        payload: dict = {
            "phoneNumber": phone_number,
            "contactName": contact_name,
            "contactPhone": contact_phone,
        }
        if contact_email:
            payload["contactEmail"] = contact_email
        if contact_org:
            payload["contactOrg"] = contact_org

        logger.info(f"Sending contact '{contact_name}' to {phone_number[:8]}...")

        response = await self._client.post(
            f"{self._base_url}/whatsapp/send-contact",
            json=payload,
        )
        data = await self._handle_response(response)
        return SendMessageResponse(
            success=data.get("success", False),
            message_id=data.get("message_id"),
        )

    async def send_image(
        self,
        phone_number: str,
        image_data: bytes,
        content_type: str = "image/jpeg",
        caption: str | None = None,
    ) -> SendMessageResponse:
        """
        Send an image.

        Args:
            phone_number: WhatsApp JID or phone number
            image_data: Image bytes
            content_type: MIME type of the image
            caption: Optional caption for the image

        Returns:
            SendMessageResponse with success status and message_id
        """
        files = {"file": ("image", image_data, content_type)}
        data: dict = {"phoneNumber": phone_number}
        if caption:
            data["caption"] = caption

        logger.info(f"Sending image to {phone_number[:8]}...")

        response = await self._client.post(
            f"{self._base_url}/whatsapp/send-image",
            files=files,
            data=data,
        )
        result = await self._handle_response(response)
        return SendMessageResponse(
            success=result.get("success", False),
            message_id=result.get("message_id"),
        )

    async def send_image_from_url(
        self,
        phone_number: str,
        image_url: str,
        caption: str | None = None,
        max_size_mb: int = 16,
    ) -> SendMessageResponse:
        """
        Download image from URL and send to WhatsApp.

        Args:
            phone_number: WhatsApp JID or phone number
            image_url: HTTPS URL of the image to download
            caption: Optional caption for the image
            max_size_mb: Maximum file size in MB (default 16)

        Returns:
            SendMessageResponse with success status and message_id

        Raises:
            WhatsAppClientError: If URL is invalid, download fails, or file too large
        """
        if not image_url.startswith("https://"):
            raise WhatsAppClientError("Only HTTPS URLs are allowed for security")

        logger.info(f"Downloading image from {image_url[:50]}...")

        try:
            img_response = await self._client.get(image_url, follow_redirects=True)
            img_response.raise_for_status()
        except httpx.HTTPError as e:
            raise WhatsAppClientError(f"Failed to download image: {e}")

        content_type = img_response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise WhatsAppClientError(f"URL does not point to an image: {content_type}")

        image_data = img_response.content
        size_mb = len(image_data) / (1024 * 1024)
        if size_mb > max_size_mb:
            raise WhatsAppClientError(f"Image too large: {size_mb:.1f}MB (max {max_size_mb}MB)")

        return await self.send_image(
            phone_number=phone_number,
            image_data=image_data,
            content_type=content_type,
            caption=caption,
        )

    async def edit_message(
        self,
        phone_number: str,
        message_id: str,
        new_text: str,
    ) -> SuccessResponse:
        """
        Edit a previously sent message.

        Args:
            phone_number: WhatsApp JID or phone number
            message_id: ID of message to edit
            new_text: New text content

        Returns:
            SuccessResponse indicating operation result
        """
        logger.info(f"Editing message {message_id[:8]}...")

        response = await self._client.post(
            f"{self._base_url}/whatsapp/edit-message",
            json={
                "phoneNumber": phone_number,
                "message_id": message_id,
                "new_text": new_text,
            },
        )
        data = await self._handle_response(response)
        return SuccessResponse(success=data.get("success", False))

    async def delete_message(
        self,
        phone_number: str,
        message_id: str,
    ) -> SuccessResponse:
        """
        Delete a message for everyone.

        Args:
            phone_number: WhatsApp JID or phone number
            message_id: ID of message to delete

        Returns:
            SuccessResponse indicating operation result
        """
        logger.info(f"Deleting message {message_id[:8]}...")

        response = await self._client.request(
            "DELETE",
            f"{self._base_url}/whatsapp/delete-message",
            json={
                "phoneNumber": phone_number,
                "message_id": message_id,
            },
        )
        data = await self._handle_response(response)
        return SuccessResponse(success=data.get("success", False))


def create_whatsapp_client(
    http_client: httpx.AsyncClient,
    base_url: str,
) -> WhatsAppClient:
    """
    Factory function for creating WhatsApp client.

    Args:
        http_client: Shared httpx.AsyncClient
        base_url: WhatsApp client REST API base URL

    Returns:
        Configured WhatsAppClient instance
    """
    return WhatsAppClient(http_client=http_client, base_url=base_url)
