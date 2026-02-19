"""
Unit tests for ai_api.transcription — validate_audio_file pure function.

Tests cover:
- Empty file rejection
- Oversized file rejection
- Unsupported extension rejection
- Valid file acceptance
- MIME type mismatch (warning but still passes)
"""

import pytest

from ai_api.transcription import (
    MAX_FILE_SIZE_BYTES,
    SUPPORTED_FORMATS,
    validate_audio_file,
)


class TestValidateAudioFile:
    """Tests for validate_audio_file(filename, content_type, file_size)."""

    # --- Empty file ---

    def test_empty_file(self):
        is_valid, error, fmt = validate_audio_file("audio.mp3", "audio/mpeg", 0)
        assert is_valid is False
        assert error == "Audio file is empty"
        assert fmt is None

    # --- File too large ---

    def test_file_too_large(self):
        oversized = MAX_FILE_SIZE_BYTES + 1
        is_valid, error, fmt = validate_audio_file("audio.mp3", "audio/mpeg", oversized)
        assert is_valid is False
        assert "File too large" in error
        assert fmt is None

    def test_file_exactly_at_limit(self):
        is_valid, error, fmt = validate_audio_file("audio.mp3", "audio/mpeg", MAX_FILE_SIZE_BYTES)
        assert is_valid is True
        assert error is None
        assert fmt == "mp3"

    # --- Unsupported extension ---

    def test_unsupported_extension(self):
        is_valid, error, fmt = validate_audio_file("audio.xyz", "audio/xyz", 1024)
        assert is_valid is False
        assert "Unsupported or missing file extension" in error
        assert fmt is None

    def test_no_extension(self):
        is_valid, error, fmt = validate_audio_file("audiofile", None, 1024)
        assert is_valid is False
        assert "Unsupported or missing file extension" in error
        assert fmt is None

    def test_empty_filename_with_dot(self):
        is_valid, error, fmt = validate_audio_file(".mp3", "audio/mpeg", 1024)
        # ".mp3" has extension "mp3" which is supported
        assert is_valid is True
        assert fmt == "mp3"

    # --- Valid files ---

    def test_valid_mp3(self):
        is_valid, error, fmt = validate_audio_file("recording.mp3", "audio/mpeg", 1024)
        assert is_valid is True
        assert error is None
        assert fmt == "mp3"

    def test_valid_ogg(self):
        is_valid, error, fmt = validate_audio_file("voice.ogg", "audio/ogg", 5000)
        assert is_valid is True
        assert error is None
        assert fmt == "ogg"

    def test_valid_wav(self):
        is_valid, error, fmt = validate_audio_file("sound.wav", "audio/wav", 10000)
        assert is_valid is True
        assert error is None
        assert fmt == "wav"

    def test_valid_flac(self):
        is_valid, error, fmt = validate_audio_file("music.flac", "audio/flac", 20000)
        assert is_valid is True
        assert error is None
        assert fmt == "flac"

    def test_valid_m4a(self):
        is_valid, error, fmt = validate_audio_file("audio.m4a", "audio/mp4", 8000)
        assert is_valid is True
        assert error is None
        assert fmt == "m4a"

    def test_valid_webm(self):
        is_valid, error, fmt = validate_audio_file("audio.webm", "audio/webm", 3000)
        assert is_valid is True
        assert error is None
        assert fmt == "webm"

    # --- All supported formats ---

    def test_all_supported_formats_accepted(self):
        for ext in SUPPORTED_FORMATS:
            filename = f"test.{ext}"
            is_valid, error, fmt = validate_audio_file(filename, None, 1024)
            assert is_valid is True, f"Format {ext} should be accepted"
            assert fmt == ext, f"Format should be '{ext}'"

    # --- MIME type handling ---

    def test_none_content_type_still_valid(self):
        is_valid, error, fmt = validate_audio_file("audio.mp3", None, 1024)
        assert is_valid is True
        assert error is None
        assert fmt == "mp3"

    def test_mime_mismatch_still_passes(self):
        # MIME mismatch logs a warning but file still passes validation
        is_valid, error, fmt = validate_audio_file("audio.mp3", "audio/ogg", 1024)
        assert is_valid is True
        assert error is None
        assert fmt == "mp3"

    def test_mime_with_codec_parameter(self):
        # MIME types can have parameters like "; codecs=opus"
        is_valid, error, fmt = validate_audio_file("audio.ogg", "audio/ogg; codecs=opus", 1024)
        assert is_valid is True
        assert error is None
        assert fmt == "ogg"

    # --- Extension case insensitivity ---

    def test_uppercase_extension(self):
        is_valid, error, fmt = validate_audio_file("audio.MP3", "audio/mpeg", 1024)
        # Extension is lowered in the code
        if "mp3" in SUPPORTED_FORMATS:
            assert is_valid is True
            assert fmt == "mp3"

    # --- Constants sanity ---

    def test_supported_formats_is_list(self):
        assert isinstance(SUPPORTED_FORMATS, list)
        assert len(SUPPORTED_FORMATS) > 0

    def test_max_file_size_positive(self):
        assert MAX_FILE_SIZE_BYTES > 0
