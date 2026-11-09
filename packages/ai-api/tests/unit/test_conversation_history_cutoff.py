"""get_conversation_history with the answered message(s) cut out (no double message).

Runs the real query on SQLite: only the two tables it reads are created, and
the pgvector column is compiled as a BLOB there.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from pgvector.sqlalchemy import Vector

# conftest.py patches sqlalchemy.create_engine; the defining module's is the real one.
from sqlalchemy.engine.create import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from ai_api.database import ConversationMessage, User, get_conversation_history


@compiles(Vector, "sqlite")
def _vector_as_blob(_type, _compiler, **_kw):
    return "BLOB"


JID = "5511999999999@s.whatsapp.net"


@pytest.fixture(autouse=True)
def _user_lookup(monkeypatch):
    """Resolve the conversation to the seeded user (identity resolution is not under test)."""

    def _lookup(db, whatsapp_jid, conversation_type, **_kw):
        return db.query(User).filter(User.whatsapp_jid == whatsapp_jid).one()

    monkeypatch.setattr("ai_api.database.get_or_create_user", _lookup)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    ConversationMessage.__table__.create(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _seed(db, contents):
    """Save one message per content, a second apart, alternating user/assistant."""
    user = User(id=uuid.uuid4(), whatsapp_jid=JID, conversation_type="private")
    db.add(user)
    db.commit()
    start = datetime(2026, 1, 1, 12, 0, 0)
    rows = []
    for i, content in enumerate(contents):
        row = ConversationMessage(
            id=uuid.uuid4(),
            user_id=user.id,
            role="user" if content.startswith("u:") else "assistant",
            content=content,
            timestamp=start + timedelta(seconds=i),
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def test_default_call_returns_everything(db):
    _seed(db, ["u:hi", "a:hello", "u:question"])
    history = get_conversation_history(db, JID, "private", limit=10)
    assert [m.content for m in history] == ["u:hi", "a:hello", "u:question"]


def test_current_message_is_not_in_its_own_history(db):
    rows = _seed(db, ["u:hi", "a:hello", "u:question"])
    current = str(rows[2].id)
    history = get_conversation_history(
        db, JID, "private", limit=10, before_message_id=current, exclude_ids=[current]
    )
    assert [m.content for m in history] == ["u:hi", "a:hello"]


def test_later_burst_messages_are_not_in_the_history(db):
    rows = _seed(db, ["u:hi", "a:hello", "u:one", "u:two", "u:three"])
    current = str(rows[2].id)  # answering "u:one" while two and three are saved
    history = get_conversation_history(
        db, JID, "private", limit=10, before_message_id=current, exclude_ids=[current]
    )
    assert [m.content for m in history] == ["u:hi", "a:hello"]


def test_a_merged_run_excludes_all_of_its_messages(db):
    rows = _seed(db, ["u:hi", "a:hello", "u:one", "u:two"])
    ids = [str(rows[2].id), str(rows[3].id)]
    history = get_conversation_history(
        db, JID, "private", limit=10, before_message_id=ids[0], exclude_ids=ids
    )
    assert [m.content for m in history] == ["u:hi", "a:hello"]


def test_limit_still_counts_from_the_cut_off(db):
    rows = _seed(db, ["u:1", "a:2", "u:3", "a:4", "u:current"])
    current = str(rows[4].id)
    history = get_conversation_history(
        db, JID, "private", limit=2, before_message_id=current, exclude_ids=[current]
    )
    assert [m.content for m in history] == ["u:3", "a:4"]


def test_malformed_or_unknown_ids_are_ignored(db):
    _seed(db, ["u:hi", "a:hello"])
    history = get_conversation_history(
        db,
        JID,
        "private",
        limit=10,
        before_message_id="not-a-uuid",
        exclude_ids=["also-bad", "00000000-0000-0000-0000-000000000000"],
    )
    assert [m.content for m in history] == ["u:hi", "a:hello"]
