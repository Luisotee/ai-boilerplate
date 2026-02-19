"""Tests for the WhatsApp HTTP client wrapper."""

import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from ai_api.whatsapp.client import (
    WhatsAppClient,
    SendMessageResponse,
    SuccessResponse,
    create_whatsapp_client,
)
from ai_api.whatsapp.exceptions import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_http_client():
    """Create a mock httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def whatsapp_client(mock_http_client):
    """Create a WhatsAppClient with mocked HTTP client."""
    return WhatsAppClient(
        http_client=mock_http_client,
        base_url="http://localhost:3001",
        api_key="test-api-key-123",
    )


# ---------------------------------------------------------------------------
# _get_headers
# ---------------------------------------------------------------------------


class TestGetHeaders:
    def test_returns_api_key_header(self, whatsapp_client):
        headers = whatsapp_client._get_headers()
        assert headers == {"X-API-Key": "test-api-key-123"}

    def test_returns_correct_key_name(self, whatsapp_client):
        headers = whatsapp_client._get_headers()
        assert "X-API-Key" in headers
        assert len(headers) == 1


# ---------------------------------------------------------------------------
# _handle_response
# ---------------------------------------------------------------------------


class TestHandleResponse:
    @pytest.mark.asyncio
    async def test_503_raises_not_connected_error(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503

        with pytest.raises(WhatsAppNotConnectedError):
            await whatsapp_client._handle_response(response)

    @pytest.mark.asyncio
    async def test_503_error_has_correct_status_code(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 503

        with pytest.raises(WhatsAppNotConnectedError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_404_raises_not_found_error(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 404
        response.json.return_value = {"error": "Phone number not found"}

        with pytest.raises(WhatsAppNotFoundError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert "Phone number not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_404_with_empty_error_uses_default(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 404
        response.json.return_value = {}

        with pytest.raises(WhatsAppNotFoundError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert "Not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_400_raises_client_error(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        response.json.return_value = {"error": "Bad request payload"}

        with pytest.raises(WhatsAppClientError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert exc_info.value.status_code == 400
        assert "Bad request payload" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_422_raises_client_error(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 422
        response.json.return_value = {"error": "Validation failed"}

        with pytest.raises(WhatsAppClientError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_500_raises_client_error(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 500
        response.json.return_value = {"error": "Internal server error"}

        with pytest.raises(WhatsAppClientError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_4xx_with_unparseable_json_falls_back_to_text(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        response.json.side_effect = ValueError("Not JSON")
        response.text = "Bad gateway"

        with pytest.raises(WhatsAppClientError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert "Bad gateway" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_4xx_with_no_text_falls_back_to_unknown(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        response.json.side_effect = ValueError("Not JSON")
        response.text = ""

        with pytest.raises(WhatsAppClientError) as exc_info:
            await whatsapp_client._handle_response(response)
        assert "Unknown error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_200_returns_json_data(self, whatsapp_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {"success": True, "message_id": "abc123"}

        result = await whatsapp_client._handle_response(response)
        assert result == {"success": True, "message_id": "abc123"}


# ---------------------------------------------------------------------------
# URL construction and method behavior
# ---------------------------------------------------------------------------


class TestURLConstruction:
    def test_base_url_trailing_slash_stripped(self):
        client = WhatsAppClient(
            http_client=MagicMock(),
            base_url="http://localhost:3001/",
            api_key="key",
        )
        assert client._base_url == "http://localhost:3001"

    @pytest.mark.asyncio
    async def test_send_text_constructs_correct_url(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "msg1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_text("5511999999999@s.whatsapp.net", "Hello")

        mock_http_client.post.assert_called_once()
        call_args = mock_http_client.post.call_args
        assert call_args[0][0] == "http://localhost:3001/whatsapp/send-text"

    @pytest.mark.asyncio
    async def test_send_reaction_constructs_correct_url(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_reaction("5511999999999@s.whatsapp.net", "msg123", "👍")

        call_args = mock_http_client.post.call_args
        assert call_args[0][0] == "http://localhost:3001/whatsapp/send-reaction"

    @pytest.mark.asyncio
    async def test_send_location_constructs_correct_url(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "loc1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_location("5511999999999@s.whatsapp.net", 40.7128, -74.0060)

        call_args = mock_http_client.post.call_args
        assert call_args[0][0] == "http://localhost:3001/whatsapp/send-location"

    @pytest.mark.asyncio
    async def test_send_contact_constructs_correct_url(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "ct1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_contact(
            "5511999999999@s.whatsapp.net", "Jane Doe", "+15551234567"
        )

        call_args = mock_http_client.post.call_args
        assert call_args[0][0] == "http://localhost:3001/whatsapp/send-contact"

    @pytest.mark.asyncio
    async def test_send_image_constructs_correct_url(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "img1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_image(
            "5511999999999@s.whatsapp.net", b"\x89PNG\r\n", "image/png"
        )

        call_args = mock_http_client.post.call_args
        assert call_args[0][0] == "http://localhost:3001/whatsapp/send-image"


# ---------------------------------------------------------------------------
# send_image_from_url
# ---------------------------------------------------------------------------


class TestSendImageFromUrl:
    @pytest.mark.asyncio
    async def test_rejects_http_urls(self, whatsapp_client):
        with pytest.raises(WhatsAppClientError, match="HTTPS"):
            await whatsapp_client.send_image_from_url(
                "5511999999999@s.whatsapp.net",
                "http://example.com/image.png",
            )

    @pytest.mark.asyncio
    async def test_rejects_non_image_content_type(self, whatsapp_client, mock_http_client):
        mock_img_response = MagicMock(spec=httpx.Response)
        mock_img_response.status_code = 200
        mock_img_response.headers = {"content-type": "text/html"}
        mock_img_response.raise_for_status = MagicMock()
        mock_http_client.get.return_value = mock_img_response

        with pytest.raises(WhatsAppClientError, match="does not point to an image"):
            await whatsapp_client.send_image_from_url(
                "5511999999999@s.whatsapp.net",
                "https://example.com/page.html",
            )

    @pytest.mark.asyncio
    async def test_rejects_oversized_image(self, whatsapp_client, mock_http_client):
        mock_img_response = MagicMock(spec=httpx.Response)
        mock_img_response.status_code = 200
        mock_img_response.headers = {"content-type": "image/jpeg"}
        mock_img_response.raise_for_status = MagicMock()
        # 17 MB image (over default 16 MB limit)
        mock_img_response.content = b"\x00" * (17 * 1024 * 1024)
        mock_http_client.get.return_value = mock_img_response

        with pytest.raises(WhatsAppClientError, match="too large"):
            await whatsapp_client.send_image_from_url(
                "5511999999999@s.whatsapp.net",
                "https://example.com/huge.jpg",
            )

    @pytest.mark.asyncio
    async def test_download_failure_raises_client_error(self, whatsapp_client, mock_http_client):
        mock_http_client.get.side_effect = httpx.HTTPError("Connection refused")

        with pytest.raises(WhatsAppClientError, match="Failed to download"):
            await whatsapp_client.send_image_from_url(
                "5511999999999@s.whatsapp.net",
                "https://example.com/missing.jpg",
            )

    @pytest.mark.asyncio
    async def test_successful_download_sends_image(self, whatsapp_client, mock_http_client):
        # Mock the download response
        mock_img_response = MagicMock(spec=httpx.Response)
        mock_img_response.status_code = 200
        mock_img_response.headers = {"content-type": "image/png"}
        mock_img_response.raise_for_status = MagicMock()
        mock_img_response.content = b"\x89PNG\r\n" * 10

        # Mock the send_image response
        mock_send_response = MagicMock(spec=httpx.Response)
        mock_send_response.status_code = 200
        mock_send_response.json.return_value = {"success": True, "message_id": "img1"}

        mock_http_client.get.return_value = mock_img_response
        mock_http_client.post.return_value = mock_send_response

        result = await whatsapp_client.send_image_from_url(
            "5511999999999@s.whatsapp.net",
            "https://example.com/photo.png",
            caption="A photo",
        )

        assert result.success is True
        assert result.message_id == "img1"


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------


class TestReturnTypes:
    @pytest.mark.asyncio
    async def test_send_text_returns_send_message_response(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "msg1"}
        mock_http_client.post.return_value = mock_response

        result = await whatsapp_client.send_text("jid@s.whatsapp.net", "Hi")
        assert isinstance(result, SendMessageResponse)
        assert result.success is True
        assert result.message_id == "msg1"

    @pytest.mark.asyncio
    async def test_send_reaction_returns_success_response(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_http_client.post.return_value = mock_response

        result = await whatsapp_client.send_reaction("jid@s.whatsapp.net", "mid", "👍")
        assert isinstance(result, SuccessResponse)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_edit_message_returns_success_response(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_http_client.post.return_value = mock_response

        result = await whatsapp_client.edit_message("jid@s.whatsapp.net", "mid", "new text")
        assert isinstance(result, SuccessResponse)

    @pytest.mark.asyncio
    async def test_delete_message_returns_success_response(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True}
        mock_http_client.request.return_value = mock_response

        result = await whatsapp_client.delete_message("jid@s.whatsapp.net", "mid")
        assert isinstance(result, SuccessResponse)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestCreateWhatsAppClient:
    def test_factory_creates_client_instance(self):
        http = MagicMock()
        client = create_whatsapp_client(http, "http://localhost:3001", "key")
        assert isinstance(client, WhatsAppClient)
        assert client._base_url == "http://localhost:3001"
        assert client._api_key == "key"


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


class TestPayloadConstruction:
    @pytest.mark.asyncio
    async def test_send_text_includes_quoted_message_id(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "msg1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_text("jid@s.whatsapp.net", "reply", quoted_message_id="orig123")

        call_kwargs = mock_http_client.post.call_args[1]
        assert call_kwargs["json"]["quoted_message_id"] == "orig123"

    @pytest.mark.asyncio
    async def test_send_text_omits_quoted_id_when_none(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "msg1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_text("jid@s.whatsapp.net", "hello")

        call_kwargs = mock_http_client.post.call_args[1]
        assert "quoted_message_id" not in call_kwargs["json"]

    @pytest.mark.asyncio
    async def test_send_location_includes_optional_fields(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "loc1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_location(
            "jid@s.whatsapp.net", 40.7, -74.0, name="NYC", address="123 Main St"
        )

        call_kwargs = mock_http_client.post.call_args[1]
        assert call_kwargs["json"]["name"] == "NYC"
        assert call_kwargs["json"]["address"] == "123 Main St"

    @pytest.mark.asyncio
    async def test_send_contact_includes_optional_fields(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "ct1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_contact(
            "jid@s.whatsapp.net",
            "Jane",
            "+1555",
            contact_email="jane@example.com",
            contact_org="Acme",
        )

        call_kwargs = mock_http_client.post.call_args[1]
        assert call_kwargs["json"]["contactEmail"] == "jane@example.com"
        assert call_kwargs["json"]["contactOrg"] == "Acme"

    @pytest.mark.asyncio
    async def test_all_requests_include_auth_headers(self, whatsapp_client, mock_http_client):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message_id": "m1"}
        mock_http_client.post.return_value = mock_response

        await whatsapp_client.send_text("jid@s.whatsapp.net", "test")

        call_kwargs = mock_http_client.post.call_args[1]
        assert call_kwargs["headers"] == {"X-API-Key": "test-api-key-123"}
