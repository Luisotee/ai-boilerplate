"""
Unit tests for ai_api.commands — pure functions and command handlers.

Tests cover:
- strip_leading_mentions
- is_command
- format_settings
- _get_help_text
- CommandResult dataclass
- Constants (SUPPORTED_LANGUAGES, LANGUAGE_NAMES, ADMIN_ONLY_COMMANDS)
- handle_clean_command (messages, data, all levels)
- _handle_memories_command (clear branch)
"""

from unittest.mock import MagicMock, patch

from ai_api.commands import (
    ADMIN_ONLY_COMMANDS,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    CommandResult,
    _get_help_text,
    _handle_memories_command,
    format_settings,
    handle_clean_command,
    is_command,
    parse_and_execute,
    strip_leading_mentions,
)

# ---------------------------------------------------------------------------
# strip_leading_mentions
# ---------------------------------------------------------------------------


class TestStripLeadingMentions:
    def test_no_mentions(self):
        assert strip_leading_mentions("hello world") == "hello world"

    def test_single_mention(self):
        assert strip_leading_mentions("@bot /settings") == "/settings"

    def test_multiple_mentions(self):
        assert strip_leading_mentions("@bot @other /help") == "/help"

    def test_mention_without_command(self):
        assert strip_leading_mentions("@bot how are you?") == "how are you?"

    def test_empty_string(self):
        assert strip_leading_mentions("") == ""

    def test_only_mention(self):
        assert strip_leading_mentions("@bot") == ""

    def test_mention_with_trailing_spaces(self):
        assert strip_leading_mentions("@bot   /tts on") == "/tts on"

    def test_mention_in_middle_not_stripped(self):
        # Only leading mentions should be stripped
        assert strip_leading_mentions("hello @bot world") == "hello @bot world"

    def test_multiple_mentions_no_space_between(self):
        # @bot@other is treated as a single token by \S+
        result = strip_leading_mentions("@bot@other /help")
        assert result == "/help"

    def test_whitespace_only(self):
        assert strip_leading_mentions("   ") == ""


# ---------------------------------------------------------------------------
# is_command
# ---------------------------------------------------------------------------


class TestIsCommand:
    def test_command_without_mention(self):
        assert is_command("/settings") is True

    def test_command_with_mention(self):
        assert is_command("@bot /settings") is True

    def test_not_a_command(self):
        assert is_command("hello world") is False

    def test_empty_string(self):
        assert is_command("") is False

    def test_slash_in_middle(self):
        assert is_command("hello /command") is False

    def test_just_slash(self):
        assert is_command("/") is True

    def test_mention_only(self):
        # After stripping @bot, the result is empty, so not a command
        assert is_command("@bot") is False

    def test_command_with_args(self):
        assert is_command("/tts on") is True

    def test_multiple_mentions_then_command(self):
        assert is_command("@bot @user /clear") is True


# ---------------------------------------------------------------------------
# format_settings
# ---------------------------------------------------------------------------


class TestFormatSettings:
    def _make_prefs(self, tts_enabled=False, tts_language="en", stt_language=None):
        prefs = MagicMock()
        prefs.tts_enabled = tts_enabled
        prefs.tts_language = tts_language
        prefs.stt_language = stt_language
        return prefs

    def test_defaults(self):
        prefs = self._make_prefs()
        result = format_settings(prefs)
        assert "TTS: disabled" in result
        assert "TTS Language: English" in result
        assert "STT Language: auto-detect" in result
        assert "/help" in result

    def test_tts_enabled(self):
        prefs = self._make_prefs(tts_enabled=True)
        result = format_settings(prefs)
        assert "TTS: enabled" in result

    def test_tts_disabled(self):
        prefs = self._make_prefs(tts_enabled=False)
        result = format_settings(prefs)
        assert "TTS: disabled" in result

    def test_tts_language_spanish(self):
        prefs = self._make_prefs(tts_language="es")
        result = format_settings(prefs)
        assert "TTS Language: Spanish" in result

    def test_tts_language_portuguese(self):
        prefs = self._make_prefs(tts_language="pt")
        result = format_settings(prefs)
        assert "TTS Language: Portuguese" in result

    def test_tts_language_french(self):
        prefs = self._make_prefs(tts_language="fr")
        result = format_settings(prefs)
        assert "TTS Language: French" in result

    def test_tts_language_german(self):
        prefs = self._make_prefs(tts_language="de")
        result = format_settings(prefs)
        assert "TTS Language: German" in result

    def test_unknown_tts_language_falls_through(self):
        prefs = self._make_prefs(tts_language="xx")
        result = format_settings(prefs)
        # When language code is not in LANGUAGE_NAMES, raw code is used
        assert "TTS Language: xx" in result

    def test_stt_language_set(self):
        prefs = self._make_prefs(stt_language="es")
        result = format_settings(prefs)
        assert "STT Language: Spanish" in result

    def test_stt_language_auto_detect(self):
        prefs = self._make_prefs(stt_language=None)
        result = format_settings(prefs)
        assert "STT Language: auto-detect" in result

    def test_stt_unknown_language_code(self):
        prefs = self._make_prefs(stt_language="xx")
        result = format_settings(prefs)
        assert "STT Language: xx" in result


# ---------------------------------------------------------------------------
# _get_help_text
# ---------------------------------------------------------------------------


class TestGetHelpText:
    def test_contains_all_commands(self):
        text = _get_help_text()
        assert "/settings" in text
        assert "/tts on" in text
        assert "/tts off" in text
        assert "/tts lang" in text
        assert "/stt lang" in text
        assert "/clean" in text
        assert "/clean data" in text
        assert "/clean all" in text
        assert "/memories" in text
        assert "/memories clear" in text
        assert "/help" in text

    def test_does_not_contain_removed_commands(self):
        text = _get_help_text()
        # /clear, /forget, /reset were replaced by /clean
        lines = text.split("\n")
        for line in lines:
            assert not line.startswith("/clear")
            assert not line.startswith("/forget")
            assert not line.startswith("/reset")

    def test_contains_language_codes(self):
        text = _get_help_text()
        assert "Language codes:" in text
        # Sorted language codes
        for lang in sorted(SUPPORTED_LANGUAGES):
            assert lang in text

    def test_returns_string(self):
        assert isinstance(_get_help_text(), str)

    def test_non_empty(self):
        assert len(_get_help_text()) > 0


# ---------------------------------------------------------------------------
# CommandResult dataclass
# ---------------------------------------------------------------------------


class TestCommandResult:
    def test_basic_creation(self):
        result = CommandResult(is_command=True)
        assert result.is_command is True
        assert result.response_text is None
        assert result.save_to_history is False

    def test_with_response(self):
        result = CommandResult(is_command=True, response_text="Done")
        assert result.response_text == "Done"

    def test_not_a_command(self):
        result = CommandResult(is_command=False)
        assert result.is_command is False
        assert result.response_text is None

    def test_save_to_history_default_false(self):
        result = CommandResult(is_command=True, response_text="test")
        assert result.save_to_history is False

    def test_save_to_history_override(self):
        result = CommandResult(is_command=True, response_text="test", save_to_history=True)
        assert result.save_to_history is True


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_supported_languages(self):
        assert SUPPORTED_LANGUAGES == {"en", "es", "pt", "fr", "de"}

    def test_language_names_keys(self):
        assert set(LANGUAGE_NAMES.keys()) == SUPPORTED_LANGUAGES

    def test_language_names_values(self):
        assert LANGUAGE_NAMES["en"] == "English"
        assert LANGUAGE_NAMES["es"] == "Spanish"
        assert LANGUAGE_NAMES["pt"] == "Portuguese"
        assert LANGUAGE_NAMES["fr"] == "French"
        assert LANGUAGE_NAMES["de"] == "German"

    def test_admin_only_commands(self):
        assert "/clean" in ADMIN_ONLY_COMMANDS
        assert "/tts" in ADMIN_ONLY_COMMANDS
        assert "/stt" in ADMIN_ONLY_COMMANDS
        assert "/settings" in ADMIN_ONLY_COMMANDS
        assert "/memories" in ADMIN_ONLY_COMMANDS
        # /help should NOT be admin-only
        assert "/help" not in ADMIN_ONLY_COMMANDS

    def test_old_commands_removed_from_admin(self):
        """Verify /clear, /forget, /reset were fully removed."""
        assert "/clear" not in ADMIN_ONLY_COMMANDS
        assert "/forget" not in ADMIN_ONLY_COMMANDS
        assert "/reset" not in ADMIN_ONLY_COMMANDS


# ---------------------------------------------------------------------------
# handle_clean_command
# ---------------------------------------------------------------------------


def _make_clean_db(message_count=0, docs=None):
    """Build a MagicMock DB session that yields message and document queries.

    The first ``db.query()`` call (ConversationMessage) returns a query whose
    ``count()`` reports ``message_count``. The second call (KnowledgeBaseDocument)
    returns a query whose ``all()`` yields ``docs``.
    """
    db = MagicMock()

    msg_query = MagicMock()
    msg_query.filter.return_value = msg_query
    msg_query.count.return_value = message_count
    msg_query.delete.return_value = message_count

    doc_query = MagicMock()
    doc_query.filter.return_value = doc_query
    doc_query.all.return_value = docs or []

    def query_side_effect(model):
        from ai_api.database import ConversationMessage

        if model is ConversationMessage:
            return msg_query
        return doc_query

    db.query.side_effect = query_side_effect
    return db


class TestHandleCleanCommandMessages:
    def test_deletes_messages(self):
        db = _make_clean_db(message_count=5)
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net")
        assert "Deleted 5 messages" in result
        db.commit.assert_called_once()

    def test_no_messages(self):
        db = _make_clean_db(message_count=0)
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net")
        assert "No messages found" in result
        db.commit.assert_called_once()

    def test_does_not_touch_documents(self):
        db = _make_clean_db(message_count=3)
        handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="messages")
        # Only the message query should have been issued — not the document query.
        db.delete.assert_not_called()

    def test_default_level_is_messages(self):
        db = _make_clean_db(message_count=2)
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net")
        assert "Deleted 2 messages" in result
        db.delete.assert_not_called()


class TestHandleCleanCommandData:
    @patch("ai_api.commands.Path.unlink")
    @patch("ai_api.commands.Path.exists", return_value=True)
    def test_deletes_messages_and_documents(self, _mock_exists, mock_unlink):
        doc1 = MagicMock()
        doc1.filename = "a.pdf"
        doc2 = MagicMock()
        doc2.filename = "b.pdf"
        db = _make_clean_db(message_count=4, docs=[doc1, doc2])

        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="data")

        assert "4 messages" in result
        assert "2 documents" in result
        assert "Conversation data cleared" in result
        assert db.delete.call_count == 2
        assert mock_unlink.call_count == 2
        db.commit.assert_called_once()

    def test_no_data(self):
        db = _make_clean_db(message_count=0, docs=[])
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="data")
        assert "No messages or documents" in result

    @patch("ai_api.commands.Path.unlink")
    @patch("ai_api.commands.Path.exists", return_value=True)
    def test_messages_only(self, _mock_exists, mock_unlink):
        db = _make_clean_db(message_count=3, docs=[])
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="data")
        assert "3 messages" in result
        assert "documents" not in result
        mock_unlink.assert_not_called()


class TestHandleCleanCommandAll:
    @patch("ai_api.commands.Path.unlink")
    @patch("ai_api.commands.Path.exists", return_value=True)
    @patch("ai_api.commands.get_or_create_preferences")
    @patch("ai_api.commands.get_or_create_core_memory")
    def test_full_reset(self, mock_get_mem, mock_get_prefs, _mock_exists, mock_unlink):
        mock_mem = MagicMock()
        mock_mem.content = "User info"
        mock_get_mem.return_value = mock_mem

        mock_prefs = MagicMock()
        mock_get_prefs.return_value = mock_prefs

        doc = MagicMock()
        doc.filename = "x.pdf"
        db = _make_clean_db(message_count=5, docs=[doc])

        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="all")

        assert "Full reset complete" in result
        assert mock_mem.content == ""
        assert mock_prefs.tts_enabled is False
        assert mock_prefs.tts_language == "en"
        assert mock_prefs.stt_language is None
        assert db.delete.call_count == 1
        assert mock_unlink.call_count == 1
        db.commit.assert_called_once()

    @patch("ai_api.commands.get_or_create_preferences")
    @patch("ai_api.commands.get_or_create_core_memory")
    def test_full_reset_with_no_data(self, mock_get_mem, mock_get_prefs):
        mock_mem = MagicMock()
        mock_mem.content = ""
        mock_get_mem.return_value = mock_mem
        mock_prefs = MagicMock()
        mock_get_prefs.return_value = mock_prefs

        db = _make_clean_db(message_count=0, docs=[])
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="all")

        assert "Full reset complete" in result
        assert mock_prefs.tts_enabled is False
        db.commit.assert_called_once()


class TestHandleCleanCommandInvalid:
    def test_invalid_level(self):
        db = MagicMock()
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="bogus")
        assert "Invalid clean level" in result
        db.commit.assert_not_called()

    def test_level_is_normalized(self):
        db = _make_clean_db(message_count=1)
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="  MESSAGES  ")
        assert "Deleted 1 messages" in result


# ---------------------------------------------------------------------------
# _handle_memories_command — clear branch
# ---------------------------------------------------------------------------


class TestHandleMemoriesClear:
    @patch("ai_api.commands.get_or_create_core_memory")
    def test_clear_with_existing_memory(self, mock_get_mem):
        mock_mem = MagicMock()
        mock_mem.content = "User info"
        mock_get_mem.return_value = mock_mem

        db = MagicMock()
        result = _handle_memories_command(db, "user-123", ["/memories", "clear"])

        assert "Core memories cleared" in result
        assert mock_mem.content == ""
        db.commit.assert_called_once()

    @patch("ai_api.commands.get_or_create_core_memory")
    def test_clear_with_empty_memory(self, mock_get_mem):
        mock_mem = MagicMock()
        mock_mem.content = ""
        mock_get_mem.return_value = mock_mem

        db = MagicMock()
        result = _handle_memories_command(db, "user-123", ["/memories", "clear"])

        assert "No core memories to clear" in result
        db.commit.assert_not_called()

    @patch("ai_api.commands.get_or_create_core_memory")
    def test_show_memories(self, mock_get_mem):
        mock_mem = MagicMock()
        mock_mem.content = "Likes pizza"
        mock_get_mem.return_value = mock_mem

        db = MagicMock()
        result = _handle_memories_command(db, "user-123", ["/memories"])

        assert "Likes pizza" in result


# ---------------------------------------------------------------------------
# /clean all empty-account branch
# ---------------------------------------------------------------------------


class TestHandleCleanCommandAllEmpty:
    @patch("ai_api.commands.get_or_create_preferences")
    @patch("ai_api.commands.get_or_create_core_memory")
    def test_returns_already_clean_when_nothing_to_reset(self, mock_get_mem, mock_get_prefs):
        mock_mem = MagicMock()
        mock_mem.content = ""
        mock_get_mem.return_value = mock_mem

        # Spec a real-shaped prefs object so attribute access returns plain
        # values instead of truthy MagicMocks (which would falsely indicate
        # "had prefs changes").
        mock_prefs = MagicMock()
        mock_prefs.tts_enabled = False
        mock_prefs.tts_language = "en"
        mock_prefs.stt_language = None
        mock_get_prefs.return_value = mock_prefs

        db = _make_clean_db(message_count=0, docs=[])
        result = handle_clean_command(db, "user-123", "123@s.whatsapp.net", level="all")

        assert "Nothing to reset" in result
        assert "already clean" in result
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# parse_and_execute — dispatcher wiring for /clean and /memories clear
# ---------------------------------------------------------------------------


class TestParseAndExecuteClean:
    def test_clean_default_level_is_messages(self):
        with patch("ai_api.commands.handle_clean_command") as mock_handle:
            mock_handle.return_value = "Deleted 0 messages."
            db = MagicMock()
            result = parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/clean")
            assert result.is_command is True
            mock_handle.assert_called_once_with(
                db, "user-123", "123@s.whatsapp.net", level="messages"
            )

    def test_clean_data_level(self):
        with patch("ai_api.commands.handle_clean_command") as mock_handle:
            mock_handle.return_value = "ok"
            db = MagicMock()
            parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/clean data")
            mock_handle.assert_called_once_with(db, "user-123", "123@s.whatsapp.net", level="data")

    def test_clean_all_level(self):
        with patch("ai_api.commands.handle_clean_command") as mock_handle:
            mock_handle.return_value = "ok"
            db = MagicMock()
            parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/clean all")
            mock_handle.assert_called_once_with(db, "user-123", "123@s.whatsapp.net", level="all")

    def test_clean_uppercase_level_normalized(self):
        with patch("ai_api.commands.handle_clean_command") as mock_handle:
            mock_handle.return_value = "ok"
            db = MagicMock()
            parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/clean DATA")
            mock_handle.assert_called_once_with(db, "user-123", "123@s.whatsapp.net", level="data")

    def test_clean_invalid_level_returns_error(self):
        # Goes through the real handler — invalid level short-circuits before any DB call.
        db = MagicMock()
        result = parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/clean bogus")
        assert result.is_command is True
        assert "Invalid clean level" in result.response_text
        db.commit.assert_not_called()

    def test_clean_blocked_for_non_admin_in_group(self):
        db = MagicMock()
        result = parse_and_execute(
            db,
            "user-123",
            "group@g.us",
            "@bot /clean data",
            conversation_type="group",
            is_group_admin=False,
        )
        assert result.is_command is True
        assert "Only group admins" in result.response_text

    def test_clean_allowed_for_admin_in_group(self):
        with patch("ai_api.commands.handle_clean_command") as mock_handle:
            mock_handle.return_value = "ok"
            db = MagicMock()
            parse_and_execute(
                db,
                "user-123",
                "group@g.us",
                "@bot /clean",
                conversation_type="group",
                is_group_admin=True,
            )
            mock_handle.assert_called_once()


class TestParseAndExecuteMemoriesClear:
    @patch("ai_api.commands.get_or_create_core_memory")
    def test_memories_clear_dispatches_to_handler(self, mock_get_mem):
        mock_mem = MagicMock()
        mock_mem.content = "Likes pizza"
        mock_get_mem.return_value = mock_mem

        db = MagicMock()
        result = parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/memories clear")

        assert result.is_command is True
        assert "Core memories cleared" in result.response_text
        assert mock_mem.content == ""
        db.commit.assert_called_once()

    @patch("ai_api.commands.get_or_create_core_memory")
    def test_memories_show_when_no_subcommand(self, mock_get_mem):
        mock_mem = MagicMock()
        mock_mem.content = "Likes pizza"
        mock_get_mem.return_value = mock_mem

        db = MagicMock()
        result = parse_and_execute(db, "user-123", "123@s.whatsapp.net", "/memories")

        assert result.is_command is True
        assert "Likes pizza" in result.response_text
