"""
Unit tests for ai_api.database — pure functions phone_from_jid, is_telegram_jid.
"""

from ai_api.database import is_telegram_jid, phone_from_jid


class TestPhoneFromJid:
    def test_valid_phone_jid(self):
        assert phone_from_jid("5491126726818@s.whatsapp.net") == "+5491126726818"

    def test_another_valid_phone_jid(self):
        assert phone_from_jid("1234567890@s.whatsapp.net") == "+1234567890"

    def test_group_jid_returns_none(self):
        assert phone_from_jid("120363012345678@g.us") is None

    def test_lid_jid_returns_none(self):
        assert phone_from_jid("12345678@lid") is None

    def test_empty_string(self):
        assert phone_from_jid("") is None

    def test_no_at_sign(self):
        assert phone_from_jid("5491126726818") is None

    def test_wrong_domain(self):
        assert phone_from_jid("5491126726818@example.com") is None

    def test_partial_domain_match(self):
        # Ensure it doesn't match partial domain strings
        assert phone_from_jid("123@s.whatsapp.net.evil") is None

    def test_just_at_domain(self):
        result = phone_from_jid("@s.whatsapp.net")
        assert result == "+"

    def test_long_phone_number(self):
        jid = "00491761234567890@s.whatsapp.net"
        assert phone_from_jid(jid) == "+00491761234567890"


class TestIsTelegramJid:
    def test_telegram_private(self):
        assert is_telegram_jid("tg:42") is True

    def test_telegram_supergroup(self):
        assert is_telegram_jid("tg:-1001234567890") is True

    def test_whatsapp_phone(self):
        assert is_telegram_jid("15551234567@s.whatsapp.net") is False

    def test_whatsapp_lid(self):
        assert is_telegram_jid("12345@lid") is False

    def test_whatsapp_group(self):
        assert is_telegram_jid("120363012345678@g.us") is False

    def test_empty_string(self):
        assert is_telegram_jid("") is False
