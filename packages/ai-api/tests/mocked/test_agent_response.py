"""Tests for agent response formatting utilities."""

import pytest
from unittest.mock import MagicMock

from ai_api.agent.response import format_message_history


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_message(role: str, content: str):
    """Create a mock database ConversationMessage object."""
    msg = MagicMock()
    msg.role = role
    msg.content = content
    return msg


# ---------------------------------------------------------------------------
# format_message_history
# ---------------------------------------------------------------------------


class TestFormatMessageHistory:
    def test_empty_list_returns_empty(self):
        result = format_message_history([])
        assert result == []

    def test_user_message_becomes_model_request(self):
        from pydantic_ai import ModelRequest, UserPromptPart

        messages = [_make_db_message("user", "Hello")]
        result = format_message_history(messages)

        assert len(result) == 1
        assert isinstance(result[0], ModelRequest)
        assert len(result[0].parts) == 1
        assert isinstance(result[0].parts[0], UserPromptPart)
        assert result[0].parts[0].content == "Hello"

    def test_assistant_message_becomes_model_response(self):
        from pydantic_ai import ModelResponse, TextPart

        messages = [_make_db_message("assistant", "Hi there!")]
        result = format_message_history(messages)

        assert len(result) == 1
        assert isinstance(result[0], ModelResponse)
        assert len(result[0].parts) == 1
        assert isinstance(result[0].parts[0], TextPart)
        assert result[0].parts[0].content == "Hi there!"

    def test_mixed_conversation_preserves_order(self):
        from pydantic_ai import ModelRequest, ModelResponse

        messages = [
            _make_db_message("user", "What is 2+2?"),
            _make_db_message("assistant", "4"),
            _make_db_message("user", "Thanks"),
            _make_db_message("assistant", "You're welcome!"),
        ]
        result = format_message_history(messages)

        assert len(result) == 4
        assert isinstance(result[0], ModelRequest)
        assert isinstance(result[1], ModelResponse)
        assert isinstance(result[2], ModelRequest)
        assert isinstance(result[3], ModelResponse)

    def test_user_message_content_is_preserved(self):
        content = "This is a longer message with special chars: !@#$%^&*()"
        messages = [_make_db_message("user", content)]
        result = format_message_history(messages)
        assert result[0].parts[0].content == content

    def test_assistant_message_content_is_preserved(self):
        content = "Here is a detailed response\nwith newlines\nand **markdown**"
        messages = [_make_db_message("assistant", content)]
        result = format_message_history(messages)
        assert result[0].parts[0].content == content

    def test_non_user_role_treated_as_assistant(self):
        """Any role that is not 'user' is formatted as ModelResponse."""
        from pydantic_ai import ModelResponse

        messages = [_make_db_message("system", "System prompt")]
        result = format_message_history(messages)

        assert len(result) == 1
        assert isinstance(result[0], ModelResponse)

    def test_single_user_message(self):
        from pydantic_ai import ModelRequest

        messages = [_make_db_message("user", "Just one question")]
        result = format_message_history(messages)
        assert len(result) == 1
        assert isinstance(result[0], ModelRequest)

    def test_multiple_consecutive_user_messages(self):
        from pydantic_ai import ModelRequest

        messages = [
            _make_db_message("user", "First"),
            _make_db_message("user", "Second"),
            _make_db_message("user", "Third"),
        ]
        result = format_message_history(messages)
        assert len(result) == 3
        assert all(isinstance(r, ModelRequest) for r in result)

    def test_multiple_consecutive_assistant_messages(self):
        from pydantic_ai import ModelResponse

        messages = [
            _make_db_message("assistant", "Response 1"),
            _make_db_message("assistant", "Response 2"),
        ]
        result = format_message_history(messages)
        assert len(result) == 2
        assert all(isinstance(r, ModelResponse) for r in result)

    def test_empty_content_is_preserved(self):
        messages = [_make_db_message("user", "")]
        result = format_message_history(messages)
        assert result[0].parts[0].content == ""

    def test_unicode_content_is_preserved(self):
        content = "Hola, como estas? Estoy bien, gracias! 日本語テスト"
        messages = [_make_db_message("user", content)]
        result = format_message_history(messages)
        assert result[0].parts[0].content == content
