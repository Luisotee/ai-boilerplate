"""services/message_events.py: who may revoke or edit a saved message, and what changes."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ai_api.services.message_events import edit_message, may_change, revoke_message

MODULE = "ai_api.services.message_events"
GROUP = "120363012345678@g.us"
PRIVATE = "5511999999999@s.whatsapp.net"
ALICE = "5511111111111@s.whatsapp.net"
BOB = "5511222222222@s.whatsapp.net"


def _row(sender_jid=None, sender_name=None, content="hi"):
    return SimpleNamespace(sender_jid=sender_jid, sender_name=sender_name, content=content)


def _db(rows):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


@pytest.mark.parametrize(
    ("row_sender", "actor", "is_group", "admin", "admin_may_act", "expected"),
    [
        (None, None, False, None, False, True),  # private chat: always
        (ALICE, ALICE, True, None, False, True),  # own message
        (ALICE, BOB, True, None, False, False),  # someone else's
        (ALICE, BOB, True, True, True, True),  # admin revoke
        (ALICE, BOB, True, True, False, False),  # admins can't edit others
        (ALICE, BOB, True, False, True, False),  # fail closed
        (ALICE, BOB, True, None, True, False),  # unknown admin status
        (None, None, True, None, True, False),  # no identities in a group
        (ALICE, None, True, None, False, False),
    ],
)
def test_may_change(row_sender, actor, is_group, admin, admin_may_act, expected):
    assert (
        may_change(
            row_sender,
            actor,
            is_group=is_group,
            is_group_admin=admin,
            admin_may_act=admin_may_act,
        )
        is expected
    )


def test_revoke_deletes_own_group_message():
    row = _row(ALICE)
    db = _db([row])
    with patch(f"{MODULE}.find_user", return_value=SimpleNamespace(id="u1")):
        assert revoke_message(db, GROUP, "M1", sender_jid=ALICE) == 1
    db.delete.assert_called_once_with(row)
    db.commit.assert_called_once()


def test_revoke_of_someone_elses_message_needs_a_confirmed_admin():
    row = _row(ALICE)
    db = _db([row])
    with patch(f"{MODULE}.find_user", return_value=SimpleNamespace(id="u1")):
        assert revoke_message(db, GROUP, "M1", sender_jid=BOB) == 0
        assert revoke_message(db, GROUP, "M1", sender_jid=BOB, is_group_admin=True) == 1
    db.delete.assert_called_once_with(row)


def test_revoke_in_a_private_chat():
    db = _db([_row()])
    with patch(f"{MODULE}.find_user", return_value=SimpleNamespace(id="u1")):
        assert revoke_message(db, PRIVATE, "M1") == 1


def test_unknown_conversation_is_a_no_op():
    db = _db([_row()])
    with patch(f"{MODULE}.find_user", return_value=None):
        assert revoke_message(db, PRIVATE, "M1") == 0
        assert edit_message(db, PRIVATE, "M1", "new") == []
    db.commit.assert_not_called()


def test_edit_rewrites_content_and_keeps_the_sender_prefix():
    row = _row(ALICE, "Alice", "Alice: old")
    db = _db([row])
    with patch(f"{MODULE}.find_user", return_value=SimpleNamespace(id="u1")):
        assert edit_message(db, GROUP, "M1", "new text", sender_jid=ALICE) == [row]
    assert row.content == "Alice: new text"
    db.commit.assert_not_called()  # the route commits after re-embedding


def test_edit_by_someone_else_is_ignored():
    row = _row(ALICE, "Alice", "Alice: old")
    db = _db([row])
    with patch(f"{MODULE}.find_user", return_value=SimpleNamespace(id="u1")):
        assert edit_message(db, GROUP, "M1", "hacked", sender_jid=BOB) == []
    assert row.content == "Alice: old"


def test_edit_in_a_private_chat_has_no_prefix():
    row = _row(content="old")
    db = _db([row])
    with patch(f"{MODULE}.find_user", return_value=SimpleNamespace(id="u1")):
        edit_message(db, PRIVATE, "M1", "new")
    assert row.content == "new"
