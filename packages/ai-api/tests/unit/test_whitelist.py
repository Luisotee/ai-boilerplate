"""
Unit tests for the whitelist check in ai_api.routes.chat._is_whitelisted.

Verifies whitelist semantics across JID formats:
- WhatsApp phone JID (`<phone>@s.whatsapp.net`) — phone-extraction match
- WhatsApp group JID (`<id>@g.us`) — full-string match
- Telegram synthetic JID (`tg:<chat_id>`) — full-string match; the
  `split("@")[0]` fallback is a no-op since there is no `@` in the string

The whitelist is read at request time via ``runtime_config.get("whitelist_phones")``
(a comma-separated string), so it can be changed through the /admin API without
a restart. These tests patch that accessor.
"""

from unittest.mock import patch

from ai_api.routes.chat import _is_whitelisted


def _patch_whitelist(value: str):
    """Patch runtime_config.get to return ``value`` for whitelist_phones."""
    return patch("ai_api.routes.chat.runtime_config.get", return_value=value)


class TestIsWhitelisted:
    def test_empty_whitelist_allows_all(self):
        with _patch_whitelist(""):
            assert _is_whitelisted("123@s.whatsapp.net") is True
            assert _is_whitelisted("tg:123") is True
            assert _is_whitelisted("anything") is True

    def test_whatsapp_phone_match(self):
        with _patch_whitelist("5491126726818"):
            assert _is_whitelisted("5491126726818@s.whatsapp.net") is True
            assert _is_whitelisted("9999999999@s.whatsapp.net") is False

    def test_whatsapp_full_jid_match(self):
        with _patch_whitelist("120363000000000000@g.us"):
            assert _is_whitelisted("120363000000000000@g.us") is True
            assert _is_whitelisted("120363111111111111@g.us") is False

    def test_telegram_full_jid_match(self):
        # For tg: JIDs there is no '@', so split("@")[0] returns the full string.
        # Whitelist entries must be the full "tg:<chat_id>" string.
        with _patch_whitelist("tg:123456789"):
            assert _is_whitelisted("tg:123456789") is True
            assert _is_whitelisted("tg:987654321") is False

    def test_telegram_group_negative_chat_id(self):
        # Telegram supergroups use negative chat IDs.
        with _patch_whitelist("tg:-1001234567890"):
            assert _is_whitelisted("tg:-1001234567890") is True
            assert _is_whitelisted("tg:-1009999999999") is False

    def test_bare_chat_id_does_not_match_tg_jid(self):
        # Entering just the digits without the tg: prefix does NOT whitelist a Telegram user.
        with _patch_whitelist("123456789"):
            assert _is_whitelisted("tg:123456789") is False

    def test_multiple_entries_comma_separated(self):
        with _patch_whitelist("5491126726818, 120363000000000000@g.us , tg:42"):
            assert _is_whitelisted("5491126726818@s.whatsapp.net") is True
            assert _is_whitelisted("120363000000000000@g.us") is True
            assert _is_whitelisted("tg:42") is True
            assert _is_whitelisted("tg:43") is False


class TestWhitelistOverridePropagation:
    """End-to-end check that an /admin override on whitelist_phones flows through
    runtime_config to the _is_whitelisted consumer (closes the consumer-side loop
    without needing a real PATCH+DB roundtrip)."""

    def test_runtime_config_override_reaches_consumer(self):
        import time

        from ai_api.routes.chat import _parse_whitelist
        from ai_api.runtime_config import runtime_config

        # Reset any cached state from earlier tests.
        _parse_whitelist.cache_clear()
        prior_overrides = runtime_config._overrides
        prior_loaded_at = runtime_config._loaded_at
        try:
            # Simulate the overlay having a DB-backed override (as if PATCH
            # /admin/settings had written it and the cache had refreshed).
            runtime_config._overrides = {"whitelist_phones": "5491126726818"}
            runtime_config._loaded_at = time.monotonic()  # keep cache fresh

            # Without the override path being wired, this would fall through to
            # the env default (empty) and accept any JID. With it, only the
            # listed phone gets through.
            assert _is_whitelisted("5491126726818@s.whatsapp.net") is True
            assert _is_whitelisted("9999999999@s.whatsapp.net") is False
        finally:
            runtime_config._overrides = prior_overrides
            runtime_config._loaded_at = prior_loaded_at
            _parse_whitelist.cache_clear()
