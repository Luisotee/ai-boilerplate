"""
Unit tests for services.autolink — Telegram phone-sharing auto-link.

This path merges a Telegram account into an existing WhatsApp `users` row
*without* a confirmation code, so it grants access to another human's
conversation history, core memories and shared-group relay authority. It must
refuse by default: there is one test per refusal condition, and the happy path
is the only route to a merge.
"""

from unittest.mock import MagicMock

from ai_api.services.autolink import (
    ERROR_ALREADY_LINKED,
    ERROR_AMBIGUOUS,
    ERROR_BAD_PHONE,
    ERROR_GROUP_JID,
    ERROR_NO_MATCH,
    ERROR_NOT_TELEGRAM,
    ERROR_UNVERIFIED_CONTACT,
    normalize_shared_phone,
    try_autolink,
)

SENDER_ID = 123456789


def _tg_user(telegram_jid=None, messages=0):
    u = MagicMock()
    u.id = "tg-uuid"
    u.whatsapp_jid = f"tg:{SENDER_ID}"
    u.telegram_jid = telegram_jid
    u.phone = None
    u.messages = [MagicMock() for _ in range(messages)]
    return u


def _wa_user(phone="+5511987654321", telegram_jid=None, uid="wa-uuid"):
    u = MagicMock()
    u.id = uid
    u.whatsapp_jid = "5511987654321@s.whatsapp.net"
    u.telegram_jid = telegram_jid
    u.phone = phone
    u.conversation_type = "private"
    u.messages = []
    return u


def _db(candidates):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = candidates
    return db


class TestNormalizeSharedPhone:
    """Telegram's `phone_number` format is undocumented beyond "the phone
    number" — the leading + is inconsistent, so normalize defensively."""

    def test_bare_digits_get_a_plus(self):
        assert normalize_shared_phone("5511987654321") == "+5511987654321"

    def test_already_plus_prefixed(self):
        assert normalize_shared_phone("+5511987654321") == "+5511987654321"

    def test_strips_formatting(self):
        assert normalize_shared_phone("+55 (11) 98765-4321") == "+5511987654321"

    def test_national_number_without_country_code_is_refused(self):
        """Refused, NOT guessed. Prepending "+" to the Brazilian national mobile
        (11) 98765-4321 yields +11987654321 — country code +1, a different
        person entirely. Matching that could link the wrong account."""
        assert normalize_shared_phone("(11) 98765-4321") is None

    def test_short_number_is_trusted_when_explicitly_international(self):
        """A leading "+" is the only reliable signal that a country code is
        present, so it lifts the length floor."""
        assert normalize_shared_phone("+3531234567") == "+3531234567"

    def test_same_digits_without_the_plus_are_refused(self):
        """The exact contrast: identical digits, no "+", so we cannot tell
        whether a country code is included."""
        assert normalize_shared_phone("3531234567") is None

    def test_absurdly_long_number_is_refused(self):
        assert normalize_shared_phone("1" * 20) is None

    def test_none_and_empty(self):
        assert normalize_shared_phone(None) is None
        assert normalize_shared_phone("") is None

    def test_letters_only_is_refused(self):
        assert normalize_shared_phone("not-a-phone") is None


class TestAutolinkRefusals:
    def test_refuses_when_caller_is_not_a_telegram_row(self):
        caller = _tg_user()
        caller.whatsapp_jid = "5511987654321@s.whatsapp.net"
        result = try_autolink(_db([]), caller, "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_NOT_TELEGRAM

    def test_refuses_when_caller_is_already_linked(self):
        caller = _tg_user(telegram_jid=f"tg:{SENDER_ID}")
        result = try_autolink(_db([]), caller, "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_ALREADY_LINKED

    def test_refuses_a_contact_card_belonging_to_someone_else(self):
        """The anti-hijack check. Sharing Alice's contact card yields ALICE's
        user_id, so requiring it to equal the sender's own id is what stops a
        user claiming someone else's phone — and their whole account."""
        db = _db([_wa_user()])
        result = try_autolink(db, _tg_user(), "+5511987654321", 999999, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_UNVERIFIED_CONTACT
        assert not db.commit.called

    def test_refuses_a_contact_with_no_user_id(self):
        """A phonebook entry for someone not on Telegram has no user_id, so it
        proves nothing about the sender."""
        result = try_autolink(_db([_wa_user()]), _tg_user(), "+5511987654321", None, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_UNVERIFIED_CONTACT

    def test_refuses_when_sender_id_is_unknown(self):
        result = try_autolink(_db([_wa_user()]), _tg_user(), "+5511987654321", SENDER_ID, None)
        assert result.success is False
        assert result.error == ERROR_UNVERIFIED_CONTACT

    def test_refuses_an_unparseable_phone(self):
        result = try_autolink(_db([_wa_user()]), _tg_user(), "12345", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_BAD_PHONE

    def test_refuses_when_no_whatsapp_row_matches(self):
        result = try_autolink(_db([]), _tg_user(), "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_NO_MATCH

    def test_refuses_when_several_rows_share_the_phone(self):
        db = _db([_wa_user(uid="a"), _wa_user(uid="b")])
        result = try_autolink(db, _tg_user(), "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_AMBIGUOUS
        assert not db.commit.called

    def test_refuses_when_the_target_is_already_linked_elsewhere(self):
        db = _db([_wa_user(telegram_jid="tg:999")])
        result = try_autolink(db, _tg_user(), "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_ALREADY_LINKED
        assert not db.commit.called

    def test_a_telegram_row_is_never_a_merge_target(self):
        """Another `tg:` row sharing the phone is not a WhatsApp identity — it
        must be filtered out, leaving no candidates rather than merging two
        Telegram accounts together."""
        other_tg = _tg_user()
        other_tg.id = "other-tg"
        other_tg.phone = "+5511987654321"
        result = try_autolink(_db([other_tg]), _tg_user(), "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_NO_MATCH

    def test_the_caller_is_never_its_own_merge_target(self):
        caller = _tg_user()
        caller.phone = "+5511987654321"
        result = try_autolink(_db([caller]), caller, "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.success is False
        assert result.error == ERROR_NO_MATCH


class TestAutolinkHappyPath:
    def test_merges_and_keeps_the_whatsapp_row(self):
        wa = _wa_user()
        caller = _tg_user()
        db = _db([wa])

        result = try_autolink(db, caller, "5511987654321", SENDER_ID, SENDER_ID)

        assert result.success is True
        # The WhatsApp row survives (it owns `phone`) and gains the Telegram id.
        assert wa.telegram_jid == f"tg:{SENDER_ID}"
        assert wa.whatsapp_jid == "5511987654321@s.whatsapp.net"
        # The Telegram orphan is dropped; cascade clears its messages/prefs.
        db.delete.assert_called_once_with(caller)
        assert db.commit.called

    def test_reports_how_many_messages_were_discarded(self):
        """The merge is irreversible and silent about data loss unless we say
        so — the reply has to name what it threw away."""
        db = _db([_wa_user()])
        result = try_autolink(db, _tg_user(messages=7), "5511987654321", SENDER_ID, SENDER_ID)

        assert result.discarded_messages == 7
        assert "7" in (result.message or "")

    def test_no_discard_note_when_there_was_no_history(self):
        db = _db([_wa_user()])
        result = try_autolink(db, _tg_user(messages=0), "5511987654321", SENDER_ID, SENDER_ID)

        assert result.discarded_messages == 0
        assert "discarded" not in (result.message or "")

    def test_rolls_back_on_commit_failure(self):
        """A failed commit leaves the route-scoped session dirty; without the
        rollback the next query raises PendingRollbackError."""
        db = _db([_wa_user()])
        db.commit.side_effect = RuntimeError("unique constraint")

        result = try_autolink(db, _tg_user(), "5511987654321", SENDER_ID, SENDER_ID)

        assert result.success is False
        assert db.rollback.called
        assert result.message and "database error" in result.message.lower()


class TestAutolinkGroupGuard:
    """A group JID must never reach the merge.

    `is_group_jid("tg:-100…")` is True but so is `is_telegram_jid(...)`, so the
    Telegram check alone lets a group through — and `_is_whitelisted` waves every
    group JID past the whitelist. Reaching the merge would `db.delete()` the
    group's row, cascading away its entire transcript, and bind the group's JID
    as a person's `telegram_jid`.
    """

    @staticmethod
    def _group_user():
        u = MagicMock()
        u.id = "group-uuid"
        u.whatsapp_jid = "tg:-1001234567890"
        u.telegram_jid = None
        u.messages = []
        return u

    def test_refuses_a_telegram_group_jid(self):
        db = _db([_wa_user()])
        result = try_autolink(db, self._group_user(), "5511987654321", SENDER_ID, SENDER_ID)

        assert result.success is False
        assert result.error == ERROR_GROUP_JID

    def test_does_not_touch_the_database(self):
        """The guard runs before any query, delete or commit."""
        db = _db([_wa_user()])
        try_autolink(db, self._group_user(), "5511987654321", SENDER_ID, SENDER_ID)

        assert not db.delete.called
        assert not db.commit.called
        assert not db.query.called

    def test_refuses_even_when_every_other_condition_is_satisfied(self):
        """Contact ids match, phone is valid, exactly one WhatsApp row matches —
        the group check is the only thing standing between this call and a
        cascade-deleted group."""
        wa = _wa_user()
        db = _db([wa])

        result = try_autolink(db, self._group_user(), "+5511987654321", SENDER_ID, SENDER_ID)

        assert result.success is False
        assert wa.telegram_jid is None

    def test_refuses_a_whatsapp_group_jid_too(self):
        u = self._group_user()
        u.whatsapp_jid = "120363012345678@g.us"
        result = try_autolink(_db([]), u, "5511987654321", SENDER_ID, SENDER_ID)

        assert result.success is False
        assert result.error == ERROR_GROUP_JID

    def test_a_private_telegram_chat_still_passes_the_guard(self):
        """Telegram private chat ids are positive, so they must not be caught."""
        result = try_autolink(_db([]), _tg_user(), "5511987654321", SENDER_ID, SENDER_ID)

        assert result.error != ERROR_GROUP_JID


class TestBoilerplateAdditions:
    def test_bare_eleven_digit_number_is_refused_but_plus_form_accepted(self):
        """Documented trade-off: a bare 11-digit number could be a national
        mobile, so +1 NANP numbers must carry their "+" (else use /link)."""
        assert normalize_shared_phone("15551234567") is None
        assert normalize_shared_phone("+15551234567") == "+15551234567"

    def test_a_linked_caller_resolving_to_its_whatsapp_row_is_already_linked(self):
        """A linked tg: JID resolves (get_or_create_user) to the kept WhatsApp
        row; the reply must say "already linked", not "Telegram only"."""
        caller = _wa_user(telegram_jid=f"tg:{SENDER_ID}")
        result = try_autolink(_db([]), caller, "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.error == ERROR_ALREADY_LINKED

    def test_a_group_row_is_never_a_merge_target(self):
        group = _wa_user(uid="grp")
        group.whatsapp_jid = "120363012345678@g.us"
        group.conversation_type = "group"
        result = try_autolink(_db([group]), _tg_user(), "+5511987654321", SENDER_ID, SENDER_ID)
        assert result.error == ERROR_NO_MATCH
