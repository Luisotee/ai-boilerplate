"""
Unit tests for ai_api.rag.conversation — pure functions only.

Tests cover:
- format_conversation_message
- format_conversation_results
- merge_and_deduplicate_messages
"""

from datetime import datetime
from unittest.mock import MagicMock

from ai_api.rag.conversation import (
    format_conversation_message,
    format_conversation_results,
    merge_and_deduplicate_messages,
)


def _make_message(
    id="msg-1",
    timestamp=None,
    role="user",
    sender_name=None,
    content="Hello",
):
    """Helper to create mock message objects."""
    msg = MagicMock()
    msg.id = id
    msg.timestamp = timestamp or datetime(2025, 1, 15, 10, 30)
    msg.role = role
    msg.sender_name = sender_name
    msg.content = content
    return msg


# ---------------------------------------------------------------------------
# format_conversation_message
# ---------------------------------------------------------------------------


class TestFormatConversationMessage:
    def test_basic_user_message(self):
        msg = _make_message(role="user", content="Hello there")
        result = format_conversation_message(msg, is_match=False)
        assert "[2025-01-15 10:30]" in result
        assert "[USER]" in result
        assert "Hello there" in result
        assert "--->" not in result

    def test_assistant_message(self):
        msg = _make_message(role="assistant", content="Hi!")
        result = format_conversation_message(msg, is_match=False)
        assert "[ASSISTANT]" in result
        assert "Hi!" in result

    def test_matched_message_has_arrow_prefix(self):
        msg = _make_message(role="user", content="Important message")
        result = format_conversation_message(msg, is_match=True)
        # The code prepends "→→→" (three Unicode right arrows)
        assert result.startswith("\u2192\u2192\u2192")

    def test_matched_message_prefix_is_first_token(self):
        msg = _make_message(role="user", content="test")
        result = format_conversation_message(msg, is_match=True)
        parts = result.split()
        assert parts[0] == "\u2192\u2192\u2192"

    def test_non_matched_no_arrow_prefix(self):
        msg = _make_message(role="user", content="test")
        result = format_conversation_message(msg, is_match=False)
        assert not result.startswith("\u2192\u2192\u2192")

    def test_with_sender_name(self):
        msg = _make_message(
            role="user",
            sender_name="Alice",
            content="Group message",
        )
        result = format_conversation_message(msg, is_match=False)
        assert "Alice: Group message" in result

    def test_without_sender_name(self):
        msg = _make_message(role="user", content="Direct message")
        msg.sender_name = None
        result = format_conversation_message(msg, is_match=False)
        assert "Direct message" in result
        # Should not have "None:" prefix
        assert "None:" not in result

    def test_timestamp_format(self):
        msg = _make_message(timestamp=datetime(2025, 12, 31, 23, 59))
        result = format_conversation_message(msg, is_match=False)
        assert "[2025-12-31 23:59]" in result

    def test_role_uppercased(self):
        msg = _make_message(role="user")
        result = format_conversation_message(msg, is_match=False)
        assert "[USER]" in result


# ---------------------------------------------------------------------------
# format_conversation_results
# ---------------------------------------------------------------------------


class TestFormatConversationResults:
    def test_empty_results(self):
        result = format_conversation_results([])
        assert result == "No relevant messages found in conversation history."

    def test_single_result_no_context(self):
        matched = _make_message(role="user", content="test message")
        results = [
            {
                "messages_before": [],
                "matched_message": matched,
                "messages_after": [],
                "similarity_score": 0.85,
            }
        ]
        result = format_conversation_results(results)
        assert "Found 1 relevant conversation snippets" in result
        assert "=== Match 1 (similarity: 0.85) ===" in result
        assert "MATCHED MESSAGE" in result
        assert "test message" in result

    def test_multiple_results(self):
        matched1 = _make_message(id="m1", content="first match")
        matched2 = _make_message(id="m2", content="second match")
        results = [
            {
                "messages_before": [],
                "matched_message": matched1,
                "messages_after": [],
                "similarity_score": 0.90,
            },
            {
                "messages_before": [],
                "matched_message": matched2,
                "messages_after": [],
                "similarity_score": 0.75,
            },
        ]
        result = format_conversation_results(results)
        assert "Found 2 relevant conversation snippets" in result
        assert "=== Match 1 (similarity: 0.90) ===" in result
        assert "=== Match 2 (similarity: 0.75) ===" in result
        assert "first match" in result
        assert "second match" in result

    def test_result_with_context_before(self):
        before_msg = _make_message(content="earlier message")
        matched = _make_message(content="matched message")
        results = [
            {
                "messages_before": [before_msg],
                "matched_message": matched,
                "messages_after": [],
                "similarity_score": 0.80,
            }
        ]
        result = format_conversation_results(results)
        assert "Context before:" in result
        assert "earlier message" in result
        assert "matched message" in result

    def test_result_with_context_after(self):
        matched = _make_message(content="matched message")
        after_msg = _make_message(content="later message")
        results = [
            {
                "messages_before": [],
                "matched_message": matched,
                "messages_after": [after_msg],
                "similarity_score": 0.80,
            }
        ]
        result = format_conversation_results(results)
        assert "Context after:" in result
        assert "later message" in result
        assert "matched message" in result

    def test_result_with_full_context(self):
        before = _make_message(content="before")
        matched = _make_message(content="match")
        after = _make_message(content="after")
        results = [
            {
                "messages_before": [before],
                "matched_message": matched,
                "messages_after": [after],
                "similarity_score": 0.95,
            }
        ]
        result = format_conversation_results(results)
        assert "Context before:" in result
        assert "Context after:" in result
        assert "before" in result
        assert "match" in result
        assert "after" in result

    def test_similarity_score_formatting(self):
        matched = _make_message(content="test")
        results = [
            {
                "messages_before": [],
                "matched_message": matched,
                "messages_after": [],
                "similarity_score": 0.123456,
            }
        ]
        result = format_conversation_results(results)
        # Should be formatted to 2 decimal places
        assert "0.12" in result


# ---------------------------------------------------------------------------
# merge_and_deduplicate_messages
# ---------------------------------------------------------------------------


class TestMergeAndDeduplicateMessages:
    def test_empty_inputs(self):
        result = merge_and_deduplicate_messages([], [])
        assert result == []

    def test_only_recent_messages(self):
        m1 = _make_message(id="1", timestamp=datetime(2025, 1, 1, 10, 0))
        m2 = _make_message(id="2", timestamp=datetime(2025, 1, 1, 11, 0))
        result = merge_and_deduplicate_messages([m1, m2], [])
        assert len(result) == 2
        assert result[0].id == "1"
        assert result[1].id == "2"

    def test_only_semantic_messages(self):
        m1 = _make_message(id="1", timestamp=datetime(2025, 1, 1, 10, 0))
        m2 = _make_message(id="2", timestamp=datetime(2025, 1, 1, 11, 0))
        result = merge_and_deduplicate_messages([], [m1, m2])
        assert len(result) == 2

    def test_deduplication(self):
        m1 = _make_message(id="shared-id", timestamp=datetime(2025, 1, 1, 10, 0))
        m2 = _make_message(id="shared-id", timestamp=datetime(2025, 1, 1, 10, 0))
        result = merge_and_deduplicate_messages([m1], [m2])
        # Should have only 1 message since IDs match
        assert len(result) == 1

    def test_no_duplicates(self):
        m1 = _make_message(id="1", timestamp=datetime(2025, 1, 1, 10, 0))
        m2 = _make_message(id="2", timestamp=datetime(2025, 1, 1, 11, 0))
        result = merge_and_deduplicate_messages([m1], [m2])
        assert len(result) == 2

    def test_sorted_by_timestamp(self):
        m_late = _make_message(id="1", timestamp=datetime(2025, 1, 1, 15, 0))
        m_early = _make_message(id="2", timestamp=datetime(2025, 1, 1, 9, 0))
        result = merge_and_deduplicate_messages([m_late], [m_early])
        assert len(result) == 2
        assert result[0].id == "2"  # earlier timestamp first
        assert result[1].id == "1"  # later timestamp second

    def test_mixed_with_duplicates_and_ordering(self):
        r1 = _make_message(id="a", timestamp=datetime(2025, 1, 1, 10, 0))
        r2 = _make_message(id="b", timestamp=datetime(2025, 1, 1, 12, 0))
        s1 = _make_message(id="b", timestamp=datetime(2025, 1, 1, 12, 0))  # duplicate
        s2 = _make_message(id="c", timestamp=datetime(2025, 1, 1, 11, 0))
        result = merge_and_deduplicate_messages([r1, r2], [s1, s2])
        assert len(result) == 3  # a, b, c (b deduplicated)
        ids = [str(m.id) for m in result]
        assert ids == ["a", "c", "b"]  # sorted by timestamp

    def test_dedup_uses_string_id(self):
        # The code uses str(msg.id) for deduplication
        import uuid

        uid = uuid.uuid4()
        m1 = _make_message(id=uid, timestamp=datetime(2025, 1, 1, 10, 0))
        m2 = _make_message(id=uid, timestamp=datetime(2025, 1, 1, 10, 0))
        result = merge_and_deduplicate_messages([m1], [m2])
        assert len(result) == 1
