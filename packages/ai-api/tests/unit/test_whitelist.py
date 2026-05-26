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
