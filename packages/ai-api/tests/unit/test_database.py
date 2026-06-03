"""
Unit tests for ai_api.database — pure functions phone_from_jid, is_telegram_jid,
plus the set_setting_overrides_batch contract.
"""

from unittest.mock import MagicMock

from ai_api.database import (
    RuntimeSetting,
    is_telegram_jid,
    phone_from_jid,
    set_setting_overrides_batch,
)


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


class TestSetSettingOverridesBatch:
    """The /admin PATCH path relies on this helper's two contracts:
    (1) inserts vs. updates correctly per key, and (2) NEVER commits — the
    route commits once after the loop so a mid-batch failure rolls back the
    whole transaction."""

    def _make_db(self, existing: dict[str, str] | None = None) -> MagicMock:
        """Mock db.query(...).filter(...).first() to return RuntimeSetting rows
        from `existing` (by key) or None when missing."""
        existing = existing or {}
        rows = {k: RuntimeSetting(key=k, value=v) for k, v in existing.items()}

        db = MagicMock()

        def fake_first():
            # The last filter() call's argument is what we're looking up; the
            # helper passes RuntimeSetting.key == key, but we don't need to
            # introspect — we just return whichever row matches the current
            # call's positional key, threaded through the mock via .filter_key.
            return rows.get(db._filter_key)

        def fake_filter(*args, **_kwargs):
            # The helper calls .filter(RuntimeSetting.key == key); we sniff the
            # key out of the BinaryExpression's right side.
            expr = args[0]
            db._filter_key = expr.right.value
            query = MagicMock()
            query.first = fake_first
            return query

        db.query.return_value.filter = fake_filter
        return db, rows

    def test_inserts_new_keys(self):
        db, rows = self._make_db()
        set_setting_overrides_batch(db, {"a": '"1"', "b": '"2"'})
        assert db.add.call_count == 2
        db.commit.assert_not_called()

    def test_updates_existing_keys_in_place(self):
        db, rows = self._make_db(existing={"a": '"old"'})
        set_setting_overrides_batch(db, {"a": '"new"'})
        db.add.assert_not_called()
        assert rows["a"].value == '"new"'
        db.commit.assert_not_called()

    def test_mixed_insert_and_update(self):
        db, rows = self._make_db(existing={"a": '"old"'})
        set_setting_overrides_batch(db, {"a": '"new"', "b": '"2"'})
        assert db.add.call_count == 1  # only b is new
        assert rows["a"].value == '"new"'
        db.commit.assert_not_called()

    def test_never_commits(self):
        """The route commits once after the batch; this helper must not."""
        db, _ = self._make_db()
        set_setting_overrides_batch(db, {"a": '"1"'})
        db.commit.assert_not_called()
