"""
Unit tests for the whitelist check in ai_api.routes.chat._is_whitelisted.

Verifies whitelist semantics across JID formats:
- WhatsApp phone JID (`<phone>@s.whatsapp.net`) — phone-extraction match
- WhatsApp group JID (`<id>@g.us`) — full-string match
- Telegram synthetic JID (`tg:<chat_id>`) — full-string match; phone-shaped
  entries live in a separate set that a `tg:` jid never consults

The whitelist is read at request time via ``runtime_config.get("whitelist_phones")``
(a comma-separated string), so it can be changed through the /admin API without
a restart. These tests patch that accessor.
"""

from unittest.mock import patch

from ai_api.routes.chat import _is_whitelisted, _parse_whitelist


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
        # A tg: jid has no '@', so only the verbatim-id clause can match it.
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


class TestLidAddressedChats:
    """Under Baileys v7 a chat is usually LID-addressed: `whatsapp_jid` is an
    anonymized `@lid` whose digits are NOT a phone. The resolved E.164 that the
    client already sends as `phone` is what lets a bare-phone entry match."""

    PHONE = "4915755945319"
    LID = "109994229891095@lid"

    def test_bare_phone_matches_lid_jid_when_phone_supplied(self):
        with _patch_whitelist(self.PHONE):
            assert _is_whitelisted(self.LID, f"+{self.PHONE}") is True

    def test_lid_without_phone_is_blocked(self):
        # Fail closed: a cold LID<->PN mapping must not open the whitelist.
        with _patch_whitelist(self.PHONE):
            assert _is_whitelisted(self.LID) is False
            assert _is_whitelisted(self.LID, None) is False

    def test_verbatim_lid_entry_is_the_escape_hatch(self):
        with _patch_whitelist(self.LID):
            assert _is_whitelisted(self.LID) is True

    def test_wrong_phone_does_not_match(self):
        with _patch_whitelist(self.PHONE):
            assert _is_whitelisted(self.LID, "+4915700000000") is False


class TestEntryFormats:
    PHONE = "4915755945319"

    def test_plus_spaced_and_suffixed_entries(self):
        for entry in (
            "4915755945319",
            "+4915755945319",
            "+49 157 5594 5319",
            "49-157-5594-5319",
            "4915755945319@s.whatsapp.net",
        ):
            with _patch_whitelist(entry):
                assert _is_whitelisted(f"{self.PHONE}@s.whatsapp.net") is True, entry
                assert _is_whitelisted("109994229891095@lid", f"+{self.PHONE}") is True, entry

    def test_device_suffixed_jid_matches(self):
        with _patch_whitelist(self.PHONE):
            assert _is_whitelisted(f"{self.PHONE}:50@s.whatsapp.net") is True

    def test_legacy_bare_group_id_still_matches(self):
        with _patch_whitelist("120363000000000000"):
            assert _is_whitelisted("120363000000000000@g.us") is True


class TestGroupScoping:
    def test_group_not_allowed_by_participant_phone(self):
        # A group has no phone of its own; a whitelisted participant must never
        # admit the whole group.
        with _patch_whitelist("4915755945319"):
            assert _is_whitelisted("120363000000000000@g.us", "+4915755945319") is False


class TestParseWhitelistCache:
    def test_cache_holds_a_single_entry(self):
        _parse_whitelist.cache_clear()
        raw = "4915755945319, tg:42"
        first = _parse_whitelist(raw)
        second = _parse_whitelist(raw)
        assert first is second
        info = _parse_whitelist.cache_info()
        assert info.hits == 1
        assert info.currsize == 1
        _parse_whitelist.cache_clear()

    def test_ids_and_phones_are_kept_separate(self):
        _parse_whitelist.cache_clear()
        wl = _parse_whitelist("4915755945319, 120363000000000000@g.us, tg:-1001234567890")
        assert wl.size == 3
        assert wl.phones == frozenset({"4915755945319"})
        assert "tg:-1001234567890" in wl.ids
        _parse_whitelist.cache_clear()


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
