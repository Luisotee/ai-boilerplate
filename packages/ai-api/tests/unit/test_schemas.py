"""
Unit tests for ai_api.schemas — Pydantic model validation.

Tests cover:
- ChatRequest: required fields, optional fields, Literal constraints
- ChatResponse
- SaveMessageRequest
- TTSRequest: Literal format constraint
- CommandResponse
- PreferencesResponse
- UpdatePreferencesRequest
"""

import pytest
from pydantic import ValidationError

from ai_api.schemas import (
    ChatRequest,
    ChatResponse,
    CommandResponse,
    PreferencesResponse,
    SaveMessageRequest,
    TTSRequest,
    UpdatePreferencesRequest,
)


# ---------------------------------------------------------------------------
# ChatRequest
# ---------------------------------------------------------------------------


class TestChatRequest:
    def test_minimal_valid(self):
        req = ChatRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
        )
        assert req.whatsapp_jid == "123@s.whatsapp.net"
        assert req.message == "Hello"
        assert req.conversation_type == "private"

    def test_group_conversation_type(self):
        req = ChatRequest(
            whatsapp_jid="123@g.us",
            message="Hi",
            conversation_type="group",
        )
        assert req.conversation_type == "group"

    def test_invalid_conversation_type(self):
        with pytest.raises(ValidationError) as exc_info:
            ChatRequest(
                whatsapp_jid="123@s.whatsapp.net",
                message="Hello",
                conversation_type="invalid",
            )
        assert "conversation_type" in str(exc_info.value)

    def test_missing_whatsapp_jid(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="Hello", conversation_type="private")

    def test_missing_message(self):
        with pytest.raises(ValidationError):
            ChatRequest(whatsapp_jid="123@s.whatsapp.net", conversation_type="private")

    def test_missing_conversation_type(self):
        with pytest.raises(ValidationError):
            ChatRequest(whatsapp_jid="123@s.whatsapp.net", message="Hello")

    def test_all_optional_fields_default_none(self):
        req = ChatRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
        )
        assert req.sender_jid is None
        assert req.sender_name is None
        assert req.whatsapp_message_id is None
        assert req.image_data is None
        assert req.image_mimetype is None
        assert req.document_data is None
        assert req.document_mimetype is None
        assert req.document_filename is None
        assert req.is_group_admin is None
        assert req.phone is None
        assert req.whatsapp_lid is None
        assert req.client_id is None

    def test_with_all_optional_fields(self):
        req = ChatRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Check this",
            conversation_type="group",
            sender_jid="456@s.whatsapp.net",
            sender_name="Alice",
            whatsapp_message_id="msg123",
            image_data="base64data",
            image_mimetype="image/jpeg",
            document_data="pdfdata",
            document_mimetype="application/pdf",
            document_filename="report.pdf",
            is_group_admin=True,
            phone="+1234567890",
            whatsapp_lid="789@lid",
            client_id="baileys",
        )
        assert req.sender_name == "Alice"
        assert req.client_id == "baileys"

    def test_client_id_baileys(self):
        req = ChatRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
            client_id="baileys",
        )
        assert req.client_id == "baileys"

    def test_client_id_cloud(self):
        req = ChatRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
            client_id="cloud",
        )
        assert req.client_id == "cloud"

    def test_client_id_invalid(self):
        with pytest.raises(ValidationError):
            ChatRequest(
                whatsapp_jid="123@s.whatsapp.net",
                message="Hello",
                conversation_type="private",
                client_id="invalid",
            )


# ---------------------------------------------------------------------------
# ChatResponse
# ---------------------------------------------------------------------------


class TestChatResponse:
    def test_valid(self):
        resp = ChatResponse(response="I can help with that!")
        assert resp.response == "I can help with that!"

    def test_missing_response(self):
        with pytest.raises(ValidationError):
            ChatResponse()

    def test_empty_response(self):
        resp = ChatResponse(response="")
        assert resp.response == ""


# ---------------------------------------------------------------------------
# SaveMessageRequest
# ---------------------------------------------------------------------------


class TestSaveMessageRequest:
    def test_minimal_valid(self):
        req = SaveMessageRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
        )
        assert req.whatsapp_jid == "123@s.whatsapp.net"

    def test_with_group_context(self):
        req = SaveMessageRequest(
            whatsapp_jid="123@g.us",
            message="Hello",
            conversation_type="group",
            sender_jid="456@s.whatsapp.net",
            sender_name="Bob",
        )
        assert req.sender_name == "Bob"

    def test_invalid_conversation_type(self):
        with pytest.raises(ValidationError):
            SaveMessageRequest(
                whatsapp_jid="123@s.whatsapp.net",
                message="Hello",
                conversation_type="dm",
            )

    def test_optional_fields_default_none(self):
        req = SaveMessageRequest(
            whatsapp_jid="123@s.whatsapp.net",
            message="Hello",
            conversation_type="private",
        )
        assert req.sender_jid is None
        assert req.sender_name is None
        assert req.whatsapp_message_id is None
        assert req.phone is None
        assert req.whatsapp_lid is None

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            SaveMessageRequest(
                whatsapp_jid="123@s.whatsapp.net",
                conversation_type="private",
            )


# ---------------------------------------------------------------------------
# TTSRequest
# ---------------------------------------------------------------------------


class TestTTSRequest:
    def test_minimal_valid(self):
        req = TTSRequest(text="Hello world")
        assert req.text == "Hello world"
        assert req.whatsapp_jid is None
        assert req.format == "ogg"  # default

    def test_with_jid(self):
        req = TTSRequest(text="Hello", whatsapp_jid="123@s.whatsapp.net")
        assert req.whatsapp_jid == "123@s.whatsapp.net"

    def test_format_ogg(self):
        req = TTSRequest(text="Hello", format="ogg")
        assert req.format == "ogg"

    def test_format_mp3(self):
        req = TTSRequest(text="Hello", format="mp3")
        assert req.format == "mp3"

    def test_format_wav(self):
        req = TTSRequest(text="Hello", format="wav")
        assert req.format == "wav"

    def test_format_flac(self):
        req = TTSRequest(text="Hello", format="flac")
        assert req.format == "flac"

    def test_invalid_format(self):
        with pytest.raises(ValidationError):
            TTSRequest(text="Hello", format="aac")

    def test_missing_text(self):
        with pytest.raises(ValidationError):
            TTSRequest()


# ---------------------------------------------------------------------------
# CommandResponse
# ---------------------------------------------------------------------------


class TestCommandResponse:
    def test_valid(self):
        resp = CommandResponse(response="Settings updated")
        assert resp.is_command is True
        assert resp.response == "Settings updated"

    def test_is_command_default_true(self):
        resp = CommandResponse(response="Done")
        assert resp.is_command is True

    def test_missing_response(self):
        with pytest.raises(ValidationError):
            CommandResponse()


# ---------------------------------------------------------------------------
# PreferencesResponse
# ---------------------------------------------------------------------------


class TestPreferencesResponse:
    def test_valid(self):
        resp = PreferencesResponse(
            tts_enabled=True,
            tts_language="en",
            stt_language="es",
        )
        assert resp.tts_enabled is True
        assert resp.tts_language == "en"
        assert resp.stt_language == "es"

    def test_stt_language_none_for_auto(self):
        resp = PreferencesResponse(
            tts_enabled=False,
            tts_language="en",
            stt_language=None,
        )
        assert resp.stt_language is None

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            PreferencesResponse(tts_enabled=True)


# ---------------------------------------------------------------------------
# UpdatePreferencesRequest
# ---------------------------------------------------------------------------


class TestUpdatePreferencesRequest:
    def test_all_none_defaults(self):
        req = UpdatePreferencesRequest()
        assert req.tts_enabled is None
        assert req.tts_language is None
        assert req.stt_language is None

    def test_update_tts_enabled(self):
        req = UpdatePreferencesRequest(tts_enabled=True)
        assert req.tts_enabled is True

    def test_update_tts_language(self):
        req = UpdatePreferencesRequest(tts_language="es")
        assert req.tts_language == "es"

    def test_update_stt_language(self):
        req = UpdatePreferencesRequest(stt_language="fr")
        assert req.stt_language == "fr"

    def test_update_all_fields(self):
        req = UpdatePreferencesRequest(
            tts_enabled=False,
            tts_language="de",
            stt_language="auto",
        )
        assert req.tts_enabled is False
        assert req.tts_language == "de"
        assert req.stt_language == "auto"
