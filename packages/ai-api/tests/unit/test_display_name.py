"""
Unit tests for ai_api.routes.chat._display_name.

This is the guard that stops a group *participant's* pushName becoming the
*group's* name — the worst failure mode of the conversation-naming feature, and
one that renames the whole conversation after whoever spoke last.
"""

from ai_api.routes.chat import _display_name
from ai_api.schemas import ChatRequest, SaveMessageRequest


def _chat(**kwargs) -> ChatRequest:
    return ChatRequest(
        whatsapp_jid=kwargs.pop("whatsapp_jid", "109994229891095@lid"),
        message=kwargs.pop("message", "hi"),
        conversation_type=kwargs.pop("conversation_type", "private"),
        **kwargs,
    )


def _save(**kwargs) -> SaveMessageRequest:
    return SaveMessageRequest(
        whatsapp_jid=kwargs.pop("whatsapp_jid", "120363012345678@g.us"),
        message=kwargs.pop("message", "hi"),
        conversation_type=kwargs.pop("conversation_type", "group"),
        **kwargs,
    )


class TestDisplayNameGroups:
    def test_group_ignores_sender_name(self):
        """The critical case: a legacy client sends only the participant's name.

        Falling back to it would rename the group after whoever spoke last, so
        nameless is the correct answer.
        """
        req = _chat(
            whatsapp_jid="120363012345678@g.us",
            conversation_type="group",
            sender_name="Ana Paula",
        )
        assert _display_name(req) is None

    def test_group_uses_profile_name_when_present(self):
        req = _chat(
            whatsapp_jid="120363012345678@g.us",
            conversation_type="group",
            sender_name="Ana Paula",
            profile_name="Equipe Terra Krya",
        )
        assert _display_name(req) == "Equipe Terra Krya"

    def test_group_with_neither_is_none(self):
        req = _chat(whatsapp_jid="120363012345678@g.us", conversation_type="group")
        assert _display_name(req) is None

    def test_save_message_request_group_ignores_sender_name(self):
        """The save-only path carries most group traffic, so it matters most."""
        assert _display_name(_save(sender_name="Ana Paula")) is None


class TestDisplayNamePrivate:
    def test_private_prefers_profile_name(self):
        req = _chat(sender_name="legacy value", profile_name="Ana Paula")
        assert _display_name(req) == "Ana Paula"

    def test_private_falls_back_to_sender_name(self):
        """In a private chat the sender *is* the conversation, so this is safe.

        It is also what lights up whatsapp-cloud/telegram and any bot that has
        not yet been redeployed with profile_name support.
        """
        assert _display_name(_chat(sender_name="Ana Paula")) == "Ana Paula"

    def test_private_with_neither_is_none(self):
        assert _display_name(_chat()) is None

    def test_save_message_request_private_falls_back(self):
        req = _save(
            whatsapp_jid="5511999999999@s.whatsapp.net",
            conversation_type="private",
            sender_name="Ana Paula",
        )
        assert _display_name(req) == "Ana Paula"
