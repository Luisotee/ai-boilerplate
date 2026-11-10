"""services/api_keys.py: key format, hashing, issue, authenticate, revoke, move."""

import hashlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from ai_api.database import ApiKey, User
from ai_api.services import api_keys
from ai_api.services.api_keys import (
    authenticate,
    create_key,
    generate_key,
    hash_key,
    list_keys,
    move_api_keys,
    revoke_key,
)


def test_key_format_and_entropy():
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        assert key.startswith("sk-")
        assert len(key) >= 3 + 43  # token_urlsafe(32) is 43 chars


def test_hash_is_sha256_hex_and_never_the_key():
    key = generate_key()
    assert hash_key(key) == hashlib.sha256(key.encode()).hexdigest()
    assert key not in hash_key(key)


class TestCreateKey:
    def test_new_web_user_and_only_the_hash_is_stored(self):
        db = MagicMock()
        issued = create_key(db, "  Ana web ")
        added = [c.args[0] for c in db.add.call_args_list]
        user = next(o for o in added if isinstance(o, User))
        record = next(o for o in added if isinstance(o, ApiKey))
        assert user.whatsapp_jid.startswith("web:")
        assert user.conversation_type == "private"
        assert user.name == "Ana web"
        assert record.key_hash == hash_key(issued.key)
        assert record.key_prefix == issued.key[:10]
        assert issued.key not in (record.key_hash, record.key_prefix)
        db.commit.assert_called_once()

    def test_attach_to_existing_private_user(self):
        existing = MagicMock(conversation_type="private", whatsapp_jid="555@s.whatsapp.net")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing
        issued = create_key(db, "Ana", jid="555@s.whatsapp.net")
        assert issued.user is existing
        assert issued.record.user_id == existing.id

    @pytest.mark.parametrize(
        "row,reason",
        [
            (None, "no user"),
            (MagicMock(conversation_type="group", whatsapp_jid="1@g.us"), "private"),
            (MagicMock(conversation_type="private", whatsapp_jid="cli:luis"), "terminal"),
        ],
    )
    def test_refusals(self, row, reason):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = row
        with pytest.raises(ValueError, match=reason):
            create_key(db, "x", jid="whatever")
        db.commit.assert_not_called()

    def test_empty_name(self):
        with pytest.raises(ValueError, match="name"):
            create_key(MagicMock(), "  ")

    def test_name_longer_than_the_column(self):
        db = MagicMock()
        create_key(db, "x" * 100)  # the limit itself is fine
        with pytest.raises(ValueError, match="too long"):
            create_key(MagicMock(), "x" * 101)


def test_list_keys_without_rows_skips_the_user_query():
    db = MagicMock()
    db.query.return_value.order_by.return_value.all.return_value = []
    assert list_keys(db) == []
    db.query.assert_called_once_with(ApiKey)


class TestAuthenticate:
    @staticmethod
    def _db(record, user):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [record, user]
        return db

    @pytest.mark.parametrize("key", [None, "", "not-a-key", "Bearer sk-x"])
    def test_malformed_keys_never_hit_the_db(self, key):
        db = MagicMock()
        assert authenticate(db, key) is None
        db.query.assert_not_called()

    def test_unknown_or_revoked_key(self):
        # The query filters on revoked_at IS NULL, so a revoked key is simply not found.
        assert authenticate(self._db(None, None), generate_key()) is None

    def test_valid_key_bumps_last_used(self):
        record = MagicMock(last_used_at=None)
        user = MagicMock()
        db = self._db(record, user)
        assert authenticate(db, generate_key()) == (record, user)
        assert record.last_used_at is not None
        db.commit.assert_called_once()

    def test_last_used_is_throttled(self):
        record = MagicMock(last_used_at=datetime.utcnow() - timedelta(seconds=5))
        db = self._db(record, MagicMock())
        assert authenticate(db, generate_key()) is not None
        db.commit.assert_not_called()

    def test_last_used_write_failure_still_authenticates(self):
        record = MagicMock(last_used_at=None)
        db = self._db(record, MagicMock())
        db.commit.side_effect = RuntimeError("db hiccup")
        assert authenticate(db, generate_key()) is not None
        db.rollback.assert_called_once()


class TestRevoke:
    def test_by_prefix(self):
        record = MagicMock(revoked_at=None, key_prefix="sk-abcdefg")
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [record]
        assert revoke_key(db, "sk-abcdefg") is record
        assert record.revoked_at is not None
        db.commit.assert_called_once()

    @pytest.mark.parametrize("pasted", ["sk-abcdefg…", "sk-abcdefg...", " sk-abcdefg… "])
    def test_prefix_copied_from_list_with_ellipsis(self, pasted):
        record = MagicMock(revoked_at=None, key_prefix="sk-abcdefg")
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [record]
        assert revoke_key(db, pasted) is record
        (expr,) = db.query.return_value.filter.call_args.args
        assert expr.right.value == "sk-abcdefg"

    def test_by_id(self):
        record = MagicMock(revoked_at=None)
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [record]
        revoke_key(db, str(uuid.uuid4()))
        assert record.revoked_at is not None

    def test_ambiguous_prefix(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [MagicMock(), MagicMock()]
        with pytest.raises(ValueError, match="share prefix"):
            revoke_key(db, "sk-a")

    def test_none(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        with pytest.raises(ValueError, match="no key"):
            revoke_key(db, "sk-zzz")

    def test_already_revoked_is_a_noop(self):
        when = datetime(2026, 1, 1)
        record = MagicMock(revoked_at=when)
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [record]
        revoke_key(db, "sk-x")
        assert record.revoked_at == when
        db.commit.assert_not_called()


def test_move_does_not_commit():
    db = MagicMock()
    db.query.return_value.filter.return_value.update.return_value = 2
    assert move_api_keys(db, uuid.uuid4(), uuid.uuid4()) == 2
    db.commit.assert_not_called()


def test_last_used_resolution_is_a_minute():
    assert api_keys.LAST_USED_RESOLUTION == timedelta(minutes=1)
