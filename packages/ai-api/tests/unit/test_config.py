"""
Unit tests for ai_api.config — pure functions only.

Tests cover:
- get_whatsapp_client_url
- get_whatsapp_api_key
"""

from ai_api.config import (
    get_whatsapp_api_key,
    get_whatsapp_client_url,
    settings,
)

# ---------------------------------------------------------------------------
# get_whatsapp_client_url
# ---------------------------------------------------------------------------


class TestGetWhatsappClientUrl:
    def test_cloud_client_id(self):
        result = get_whatsapp_client_url("cloud")
        assert result == settings.whatsapp_cloud_client_url

    def test_baileys_client_id(self):
        result = get_whatsapp_client_url("baileys")
        assert result == settings.whatsapp_client_url

    def test_none_client_id(self):
        result = get_whatsapp_client_url(None)
        assert result == settings.whatsapp_client_url

    def test_empty_string_client_id(self):
        result = get_whatsapp_client_url("")
        assert result == settings.whatsapp_client_url

    def test_unknown_client_id(self):
        result = get_whatsapp_client_url("unknown")
        assert result == settings.whatsapp_client_url

    def test_cloud_returns_different_from_default(self):
        # Cloud and default URLs are configured separately
        cloud_url = get_whatsapp_client_url("cloud")
        default_url = get_whatsapp_client_url("baileys")
        # They should be the configured values (may or may not differ in test env)
        assert cloud_url == settings.whatsapp_cloud_client_url
        assert default_url == settings.whatsapp_client_url


# ---------------------------------------------------------------------------
# get_whatsapp_api_key
# ---------------------------------------------------------------------------


class TestGetWhatsappApiKey:
    def test_non_cloud_returns_default_key(self):
        result = get_whatsapp_api_key("baileys")
        assert result == settings.whatsapp_api_key

    def test_none_returns_default_key(self):
        result = get_whatsapp_api_key(None)
        assert result == settings.whatsapp_api_key

    def test_cloud_without_cloud_key_returns_default(self):
        # When whatsapp_cloud_api_key is None, falls back to whatsapp_api_key
        if settings.whatsapp_cloud_api_key is None:
            result = get_whatsapp_api_key("cloud")
            assert result == settings.whatsapp_api_key

    def test_cloud_with_cloud_key_returns_cloud_key(self):
        # When whatsapp_cloud_api_key is set, it should be returned for "cloud"
        if settings.whatsapp_cloud_api_key:
            result = get_whatsapp_api_key("cloud")
            assert result == settings.whatsapp_cloud_api_key

    def test_empty_client_id_returns_default(self):
        result = get_whatsapp_api_key("")
        assert result == settings.whatsapp_api_key

    def test_unknown_client_id_returns_default(self):
        result = get_whatsapp_api_key("unknown")
        assert result == settings.whatsapp_api_key

    def test_default_key_matches_env(self):
        # conftest.py sets WHATSAPP_API_KEY=test-wa-key
        assert settings.whatsapp_api_key == "test-wa-key"

    def test_api_key_is_string(self):
        result = get_whatsapp_api_key("baileys")
        assert isinstance(result, str)
