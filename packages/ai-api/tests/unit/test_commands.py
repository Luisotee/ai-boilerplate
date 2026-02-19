"""
Unit tests for ai_api.commands — pure functions only.

Tests cover:
- strip_leading_mentions
- is_command
- _parse_duration
- format_settings
- _get_help_text
- CommandResult dataclass
- Constants (SUPPORTED_LANGUAGES, LANGUAGE_NAMES, ADMIN_ONLY_COMMANDS)
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from ai_api.commands import (
    ADMIN_ONLY_COMMANDS,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    CommandResult,
    _get_help_text,
    _parse_duration,
    format_settings,
    is_command,
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
        assert is_command("@bot @user /clean 1h") is True


# ---------------------------------------------------------------------------
# _parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_hours(self):
        assert _parse_duration("1h") == timedelta(hours=1)

    def test_multiple_hours(self):
        assert _parse_duration("24h") == timedelta(hours=24)

    def test_days(self):
        assert _parse_duration("7d") == timedelta(days=7)

    def test_single_day(self):
        assert _parse_duration("1d") == timedelta(days=1)

    def test_months(self):
        assert _parse_duration("1m") == timedelta(days=30)

    def test_multiple_months(self):
        assert _parse_duration("3m") == timedelta(days=90)

    def test_case_insensitive(self):
        assert _parse_duration("1H") == timedelta(hours=1)
        assert _parse_duration("7D") == timedelta(days=7)
        assert _parse_duration("1M") == timedelta(days=30)

    def test_invalid_unit(self):
        assert _parse_duration("1x") is None

    def test_no_number(self):
        assert _parse_duration("h") is None

    def test_empty_string(self):
        assert _parse_duration("") is None

    def test_float_not_supported(self):
        assert _parse_duration("1.5h") is None

    def test_negative_not_supported(self):
        assert _parse_duration("-1h") is None

    def test_zero_hours(self):
        assert _parse_duration("0h") == timedelta(hours=0)

    def test_large_value(self):
        assert _parse_duration("100d") == timedelta(days=100)

    def test_random_text(self):
        assert _parse_duration("abc") is None

    def test_number_only(self):
        assert _parse_duration("42") is None


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
        assert "/memories" in text
        assert "/help" in text

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
