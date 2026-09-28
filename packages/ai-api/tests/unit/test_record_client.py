"""routes/chat.py _record_client: the per-user routing columns broadcasts rely on."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from ai_api.routes.chat import _record_client


def _user(**kw):
    base = dict(last_client_id=None, whatsapp_client_id=None, cloud_last_inbound_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_first_baileys_message_sets_both_client_columns():
    user, db = _user(), MagicMock()
    _record_client(db, user, None)  # an omitted client_id means Baileys
    assert user.last_client_id == "baileys"
    assert user.whatsapp_client_id == "baileys"
    assert user.cloud_last_inbound_at is None
    db.commit.assert_called_once()


def test_unchanged_baileys_user_writes_nothing():
    user, db = _user(last_client_id="baileys", whatsapp_client_id="baileys"), MagicMock()
    _record_client(db, user, "baileys")
    db.commit.assert_not_called()


def test_cloud_message_always_bumps_the_window_timestamp():
    user, db = _user(last_client_id="cloud", whatsapp_client_id="cloud"), MagicMock()
    _record_client(db, user, "cloud")
    assert user.cloud_last_inbound_at is not None
    assert user.cloud_last_inbound_at.tzinfo is None  # naive UTC, like the DB
    db.commit.assert_called_once()


def test_telegram_message_keeps_the_whatsapp_client():
    """A linked Cloud user who writes on Telegram must still be reached via Cloud."""
    user, db = _user(last_client_id="cloud", whatsapp_client_id="cloud"), MagicMock()
    _record_client(db, user, "telegram")
    assert user.last_client_id == "telegram"
    assert user.whatsapp_client_id == "cloud"


def test_commit_failure_is_swallowed_and_rolled_back():
    user, db = _user(), MagicMock()
    db.commit.side_effect = RuntimeError("db down")
    _record_client(db, user, "cloud")  # must not raise: the reply matters more
    db.rollback.assert_called_once()
