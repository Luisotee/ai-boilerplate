"""
Unit tests for ai_api.tts — pure functions only.

Tests cover:
- validate_text_input
- get_voice_for_language
- get_audio_mimetype
- TTS_VOICES and AUDIO_FORMATS constants
"""

import pytest

from ai_api.config import settings
from ai_api.tts import (
    AUDIO_FORMATS,
    TTS_VOICES,
    get_audio_mimetype,
    get_voice_for_language,
    validate_text_input,
)


# ---------------------------------------------------------------------------
# validate_text_input
# ---------------------------------------------------------------------------


class TestValidateTextInput:
    def test_empty_string(self):
        is_valid, error = validate_text_input("")
        assert is_valid is False
        assert error == "Text is empty"

    def test_whitespace_only(self):
        is_valid, error = validate_text_input("   ")
        assert is_valid is False
        assert error == "Text contains only whitespace"

    def test_tabs_only(self):
        is_valid, error = validate_text_input("\t\t")
        assert is_valid is False
        assert error == "Text contains only whitespace"

    def test_newlines_only(self):
        is_valid, error = validate_text_input("\n\n\n")
        assert is_valid is False
        assert error == "Text contains only whitespace"

    def test_mixed_whitespace_only(self):
        is_valid, error = validate_text_input("  \t\n  ")
        assert is_valid is False
        assert error == "Text contains only whitespace"

    def test_valid_text(self):
        is_valid, error = validate_text_input("Hello, world!")
        assert is_valid is True
        assert error is None

    def test_short_text(self):
        is_valid, error = validate_text_input("a")
        assert is_valid is True
        assert error is None

    def test_text_at_max_length(self):
        text = "x" * settings.tts_max_text_length
        is_valid, error = validate_text_input(text)
        assert is_valid is True
        assert error is None

    def test_text_over_max_length(self):
        text = "x" * (settings.tts_max_text_length + 1)
        is_valid, error = validate_text_input(text)
        assert is_valid is False
        assert "Text too long" in error
        assert str(len(text)) in error
        assert str(settings.tts_max_text_length) in error

    def test_text_way_over_max_length(self):
        text = "x" * (settings.tts_max_text_length * 2)
        is_valid, error = validate_text_input(text)
        assert is_valid is False
        assert "Text too long" in error

    def test_text_with_unicode(self):
        is_valid, error = validate_text_input("Hola, como estas?")
        assert is_valid is True
        assert error is None


# ---------------------------------------------------------------------------
# get_voice_for_language
# ---------------------------------------------------------------------------


class TestGetVoiceForLanguage:
    def test_english(self):
        assert get_voice_for_language("en") == "Kore"

    def test_spanish(self):
        assert get_voice_for_language("es") == "Aoede"

    def test_portuguese(self):
        assert get_voice_for_language("pt") == "Puck"

    def test_french(self):
        assert get_voice_for_language("fr") == "Charon"

    def test_german(self):
        assert get_voice_for_language("de") == "Fenrir"

    def test_unknown_language_fallback(self):
        # Unknown language should fall back to settings.tts_default_voice
        result = get_voice_for_language("xx")
        assert result == settings.tts_default_voice

    def test_empty_language_fallback(self):
        result = get_voice_for_language("")
        assert result == settings.tts_default_voice

    def test_all_tts_voices_present(self):
        expected_languages = {"en", "es", "pt", "fr", "de"}
        assert set(TTS_VOICES.keys()) == expected_languages


# ---------------------------------------------------------------------------
# get_audio_mimetype
# ---------------------------------------------------------------------------


class TestGetAudioMimetype:
    def test_ogg(self):
        assert get_audio_mimetype("ogg") == "audio/ogg"

    def test_mp3(self):
        assert get_audio_mimetype("mp3") == "audio/mpeg"

    def test_wav(self):
        assert get_audio_mimetype("wav") == "audio/wav"

    def test_flac(self):
        assert get_audio_mimetype("flac") == "audio/flac"

    def test_unknown_format_fallback(self):
        # Unknown format should fall back to "ogg" format config
        result = get_audio_mimetype("aac")
        assert result == "audio/ogg"

    def test_empty_format_fallback(self):
        result = get_audio_mimetype("")
        assert result == "audio/ogg"


# ---------------------------------------------------------------------------
# AUDIO_FORMATS constant
# ---------------------------------------------------------------------------


class TestAudioFormats:
    def test_ogg_format_config(self):
        fmt, codec, mime = AUDIO_FORMATS["ogg"]
        assert fmt == "ogg"
        assert codec == "libopus"
        assert mime == "audio/ogg"

    def test_mp3_format_config(self):
        fmt, codec, mime = AUDIO_FORMATS["mp3"]
        assert fmt == "mp3"
        assert codec is None
        assert mime == "audio/mpeg"

    def test_wav_format_config(self):
        fmt, codec, mime = AUDIO_FORMATS["wav"]
        assert fmt == "wav"
        assert codec is None
        assert mime == "audio/wav"

    def test_flac_format_config(self):
        fmt, codec, mime = AUDIO_FORMATS["flac"]
        assert fmt == "flac"
        assert codec is None
        assert mime == "audio/flac"

    def test_all_formats_present(self):
        assert set(AUDIO_FORMATS.keys()) == {"ogg", "mp3", "wav", "flac"}

    def test_all_formats_are_tuples_of_three(self):
        for key, value in AUDIO_FORMATS.items():
            assert len(value) == 3, f"Format {key} should have 3 elements"
