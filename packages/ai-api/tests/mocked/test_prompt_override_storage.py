"""Prompt override storage: the upsert, and the legacy bot_prompt check at startup."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects import postgresql

from ai_api import database


def test_set_prompt_override_is_one_upsert():
    """Two first saves of one key must not race into an IntegrityError."""
    db = MagicMock()
    database.set_prompt_override(db, "tone", "Tone: brief.", "abc")
    stmt = db.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "INSERT INTO bot_prompt_overrides" in sql
    assert "ON CONFLICT (key) DO UPDATE" in sql
    db.commit.assert_called_once()


def test_only_legacy_rows_with_text_are_reported():
    rows = [MagicMock(content="A real persona."), MagicMock(content="  "), MagicMock(content="")]
    db = MagicMock()
    db.query.return_value.all.return_value = rows
    assert database.custom_legacy_prompt_rows(db) == [rows[0]]


def test_startup_warns_about_a_legacy_row():
    from ai_api import main

    row = MagicMock(content="A real persona.", updated_at=datetime(2026, 6, 1))
    with (
        patch.object(main, "SessionLocal", MagicMock()),
        patch.object(main, "load_resolved_prompt", return_value=MagicMock(text="")),
        patch.object(main, "custom_legacy_prompt_rows", return_value=[row]),
        patch.object(main.logger, "warning") as warning,
    ):
        main._warn_on_stale_prompt()
    assert "legacy bot_prompt" in warning.call_args.args[0]


def test_the_core_memory_read_creates_nothing():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    assert database.get_core_memory(db, "u") is None
    db.add.assert_not_called()
