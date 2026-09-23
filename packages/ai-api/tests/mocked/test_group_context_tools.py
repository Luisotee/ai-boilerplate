"""
Mocked tests for the shared-group tools (agent/tools/group_context.py):
get_group_context (read) and send_group_message (relay).

Ported from curupira's test_get_group_context.py / test_send_group_message.py.
The privacy contract under test:
  * requester identity comes from the DB row, never from tool arguments;
  * only groups on the freshly verified shared list can be read or targeted;
  * sends match strictly and always carry an attribution line;
  * Telegram re-checks membership live before a send and FAILS CLOSED;
  * group chats and the Cloud API are refused gracefully.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_api.agent.tools.group_context import (
    CLOUD_UNSUPPORTED,
    get_group_context,
    send_group_message,
    shared_group_tools_enabled,
)
from ai_api.config import settings
from ai_api.whatsapp import WhatsAppNotConnectedError
from ai_api.whatsapp.client import SendMessageResponse, SharedGroup
from tests.helpers.factories import make_conversation_message, make_user

MODULE = "ai_api.agent.tools.group_context"
SECRET = "postgresql://admin:hunter2@db.internal:5432/prod"

BOOK = SharedGroup(group_jid="120363000000001@g.us", subject="Book Club")
HIKE = SharedGroup(group_jid="120363000000002@g.us", subject="Hiking Crew")
HIKE2 = SharedGroup(group_jid="120363000000003@g.us", subject="Hiking Planning")


def _make_ctx(
    *,
    whatsapp_jid: str = "5511999999999@s.whatsapp.net",
    client_id: str | None = None,
    shared_groups: list | None = None,
    with_client: bool = True,
    with_embeddings: bool = True,
    user=None,
) -> MagicMock:
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = whatsapp_jid
    ctx.deps.client_id = client_id
    requester = (
        user
        if user is not None
        else make_user(
            whatsapp_jid,
            name="Ana",
            phone="+5511999999999",
            whatsapp_lid="70253400879283@lid",
        )
    )
    ctx.deps.db.query.return_value.filter.return_value.first.return_value = requester

    if with_client:
        ctx.deps.whatsapp_client = MagicMock()
        ctx.deps.whatsapp_client.get_shared_groups = AsyncMock(
            return_value=shared_groups if shared_groups is not None else []
        )
        ctx.deps.whatsapp_client.send_text = AsyncMock(
            return_value=SendMessageResponse(success=True, message_id="M1")
        )
        ctx.deps.whatsapp_client.is_group_member = AsyncMock(return_value=True)
    else:
        ctx.deps.whatsapp_client = None

    if with_embeddings:
        ctx.deps.embedding_service = MagicMock()
        ctx.deps.embedding_service.generate = AsyncMock(return_value=[0.1, 0.2])
    else:
        ctx.deps.embedding_service = None
    return ctx


@pytest.fixture(autouse=True)
def _env_runtime_config():
    with patch(f"{MODULE}.runtime_config") as mock_rc:
        mock_rc.get.side_effect = lambda key: getattr(settings, key)
        yield mock_rc


@pytest.fixture
def group_row():
    row = make_user(BOOK.group_jid, conversation_type="group", name="Book Club")
    row.id = "group-row-1"
    with patch(f"{MODULE}._group_row", return_value=row) as m:
        yield m


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    async def test_tools_hidden_when_disabled(self, _env_runtime_config):
        _env_runtime_config.get.side_effect = lambda key: False
        tool_def = MagicMock()
        assert await shared_group_tools_enabled(MagicMock(), tool_def) is None

    async def test_tools_visible_when_enabled(self, _env_runtime_config):
        _env_runtime_config.get.side_effect = lambda key: True
        tool_def = MagicMock()
        assert await shared_group_tools_enabled(MagicMock(), tool_def) is tool_def

    def test_default_is_off(self):
        from ai_api.config import Settings

        assert Settings.model_fields["shared_group_tools_enabled"].default is False

    def test_both_tools_registered_with_the_prepare_hook(self):
        from ai_api.agent.core import agent

        tools = agent._function_toolset.tools
        for name in ("get_group_context", "send_group_message"):
            assert tools[name].prepare is shared_group_tools_enabled


# ---------------------------------------------------------------------------
# get_group_context
# ---------------------------------------------------------------------------


class TestReadGuards:
    @pytest.mark.parametrize("jid", ["120363012345678@g.us", "tg:-1001234567890"])
    async def test_group_chat_is_refused(self, jid):
        ctx = _make_ctx(whatsapp_jid=jid)
        result = await get_group_context(ctx)
        assert "private chat" in result
        ctx.deps.whatsapp_client.get_shared_groups.assert_not_called()

    async def test_cloud_api_is_refused_gracefully(self):
        ctx = _make_ctx(client_id="cloud")
        assert await get_group_context(ctx) == CLOUD_UNSUPPORTED
        ctx.deps.whatsapp_client.get_shared_groups.assert_not_called()

    async def test_no_shared_groups(self):
        ctx = _make_ctx(shared_groups=[])
        assert "both in" in await get_group_context(ctx)


class TestDiscovery:
    async def test_lists_shared_groups_using_db_identity(self):
        ctx = _make_ctx(shared_groups=[BOOK, HIKE])
        result = await get_group_context(ctx)
        assert "- Book Club" in result and "- Hiking Crew" in result
        ctx.deps.whatsapp_client.get_shared_groups.assert_awaited_once_with(
            jid="5511999999999@s.whatsapp.net",
            lid="70253400879283@lid",
            phone="+5511999999999",
        )

    async def test_telegram_lists_groups_derived_from_the_db(self):
        ctx = _make_ctx(whatsapp_jid="tg:555", client_id="telegram")
        derived = [SharedGroup(group_jid="tg:-1001", subject="Team chat")]
        with patch(
            "ai_api.services.shared_groups.derive_telegram_shared_groups",
            return_value=derived,
        ) as mock_derive:
            result = await get_group_context(ctx)
        assert "- Team chat" in result
        mock_derive.assert_called_once()
        ctx.deps.whatsapp_client.get_shared_groups.assert_not_called()


class TestTranscriptMode:
    @patch(f"{MODULE}.get_conversation_messages")
    async def test_exact_match_returns_transcript_labelled_with_bot_name(
        self, mock_msgs, group_row, _env_runtime_config
    ):
        _env_runtime_config.get.side_effect = lambda key: {"bot_name": "Jarvis"}[key]
        mock_msgs.return_value = [
            make_conversation_message("user", "Bob: next book?", sender_name="Bob"),
            make_conversation_message("assistant", "Dune!"),
        ]
        ctx = _make_ctx(shared_groups=[BOOK, HIKE])
        result = await get_group_context(ctx, group_name="book club")

        assert 'Recent activity in "Book Club"' in result
        assert "Bob: next book?" in result and "Bob: Bob:" not in result
        assert "Jarvis: Dune!" in result
        # Scoped to the matched group's row, looked up without creating one.
        group_row.assert_called_once_with(ctx.deps.db, BOOK.group_jid)
        assert mock_msgs.call_args.args == (ctx.deps.db, "group-row-1")

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_fuzzy_match_for_reads(self, mock_msgs, group_row):
        mock_msgs.return_value = [make_conversation_message("user", "hi")]
        ctx = _make_ctx(shared_groups=[BOOK])
        result = await get_group_context(ctx, group_name="Bok Clb")
        assert "Book Club" in result

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_unknown_name_lists_available_and_reads_nothing(self, mock_msgs, group_row):
        ctx = _make_ctx(shared_groups=[BOOK])
        result = await get_group_context(ctx, group_name="Secret Society")
        assert "Book Club" in result
        mock_msgs.assert_not_called()
        group_row.assert_not_called()

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_no_row_means_no_messages(self, mock_msgs):
        with patch(f"{MODULE}._group_row", return_value=None):
            result = await get_group_context(_make_ctx(shared_groups=[BOOK]), group_name="Book")
        assert "don't have any stored messages" in result
        mock_msgs.assert_not_called()

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_limit_is_capped(self, mock_msgs, group_row):
        mock_msgs.return_value = [make_conversation_message("user", "hi")]
        await get_group_context(_make_ctx(shared_groups=[BOOK]), group_name="Book", limit=10_000)
        assert mock_msgs.call_args.kwargs["limit"] == 100

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_since_hours_is_a_naive_utc_window(self, mock_msgs, group_row):
        mock_msgs.return_value = [
            make_conversation_message("user", "hi", timestamp=datetime(2026, 6, 5, 14, 30))
        ]
        result = await get_group_context(
            _make_ctx(shared_groups=[BOOK]), group_name="Book", since_hours=24
        )
        since = mock_msgs.call_args.kwargs["since"]
        assert since.tzinfo is None
        expected = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
        assert abs((since - expected).total_seconds()) < 60
        assert "[2026-06-05 14:30]" in result


class TestSearchMode:
    @patch(f"{MODULE}.format_conversation_results", return_value="FORMATTED")
    @patch(f"{MODULE}.search_conversation_fn", new_callable=AsyncMock)
    async def test_search_is_scoped_to_the_group_row(self, mock_search, _fmt, group_row):
        mock_search.return_value = [{"matched_message": MagicMock()}]
        ctx = _make_ctx(shared_groups=[BOOK])
        result = await get_group_context(ctx, group_name="Book Club", search_query="dune")
        assert "FORMATTED" in result
        assert mock_search.call_args.kwargs["user_id"] == "group-row-1"

    @patch(f"{MODULE}.get_conversation_messages")
    async def test_search_without_embeddings_says_so(self, mock_msgs, group_row):
        ctx = _make_ctx(shared_groups=[BOOK], with_embeddings=False)
        result = await get_group_context(ctx, group_name="Book", search_query="dune")
        assert "isn't available" in result
        mock_msgs.assert_not_called()


class TestReadErrors:
    async def test_exception_rolls_back_and_hides_details(self):
        ctx = _make_ctx()
        ctx.deps.whatsapp_client.get_shared_groups.side_effect = RuntimeError(SECRET)
        result = await get_group_context(ctx)
        assert SECRET not in result and "hunter2" not in result
        ctx.deps.db.rollback.assert_called_once()

    async def test_disconnected_client_is_truthful(self):
        ctx = _make_ctx()
        ctx.deps.whatsapp_client.get_shared_groups.side_effect = WhatsAppNotConnectedError()
        assert "disconnected" in await get_group_context(ctx)


# ---------------------------------------------------------------------------
# send_group_message
# ---------------------------------------------------------------------------


class TestSendGuards:
    @pytest.mark.parametrize("jid", ["120363012345678@g.us", "tg:-1001234567890"])
    async def test_group_chat_is_refused(self, jid):
        ctx = _make_ctx(whatsapp_jid=jid, shared_groups=[BOOK])
        result = await send_group_message(ctx, "Book Club", "hi")
        assert "private chat" in result
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_cloud_api_is_refused_gracefully(self):
        ctx = _make_ctx(client_id="cloud", shared_groups=[BOOK])
        assert await send_group_message(ctx, "Book Club", "hi") == CLOUD_UNSUPPORTED
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_empty_message_and_group_are_refused_before_lookup(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        assert "empty" in await send_group_message(ctx, "Book Club", "   ")
        assert "Which group" in await send_group_message(ctx, "  ", "hi")
        ctx.deps.whatsapp_client.get_shared_groups.assert_not_called()


class TestSendAuthorization:
    async def test_no_shared_groups_does_not_send(self):
        ctx = _make_ctx(shared_groups=[])
        await send_group_message(ctx, "Book Club", "hi")
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_unlisted_group_is_never_targeted(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        result = await send_group_message(ctx, "120363999999999@g.us", "hi")
        assert "Book Club" in result
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_ambiguous_name_asks_instead_of_guessing(self):
        ctx = _make_ctx(shared_groups=[HIKE, HIKE2])
        result = await send_group_message(ctx, "hiking", "hi")
        assert "Which one" in result
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_typo_is_not_fuzzy_matched_for_a_send(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        await send_group_message(ctx, "Bok Clb", "hi")
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_unknown_requester_does_not_send(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        ctx.deps.db.query.return_value.filter.return_value.first.return_value = None
        assert "identify" in await send_group_message(ctx, "Book Club", "hi")
        ctx.deps.whatsapp_client.send_text.assert_not_called()


class TestSend:
    async def test_sends_to_the_group_jid_with_attribution(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        result = await send_group_message(ctx, "book club", "See you at 8")
        ctx.deps.whatsapp_client.send_text.assert_awaited_once_with(
            phone_number=BOOK.group_jid,
            text="📩 Ana (+5511999999999) asked me to send this message:\n\nSee you at 8",
        )
        assert 'sent your message to "Book Club"' in result

    async def test_markdown_body_is_converted(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        await send_group_message(ctx, "Book Club", "**bold**")
        assert ctx.deps.whatsapp_client.send_text.call_args.kwargs["text"].endswith("*bold*")

    async def test_attribution_uses_sender_name_arg_and_phone_only(self):
        user = make_user("5511999999999@s.whatsapp.net", name=None, phone="+5511999999999")
        ctx = _make_ctx(shared_groups=[BOOK], user=user)
        await send_group_message(ctx, "Book Club", "hi")
        assert ctx.deps.whatsapp_client.send_text.call_args.kwargs["text"].startswith(
            "📩 +5511999999999 asked me"
        )
        await send_group_message(ctx, "Book Club", "hi", sender_name="Bea")
        assert ctx.deps.whatsapp_client.send_text.call_args.kwargs["text"].startswith(
            "📩 Bea (+5511999999999) asked me"
        )

    async def test_refuses_when_neither_name_nor_phone_is_known(self):
        user = make_user("70253400879283@lid", name=None, phone=None)
        ctx = _make_ctx(whatsapp_jid="70253400879283@lid", shared_groups=[BOOK], user=user)
        result = await send_group_message(ctx, "Book Club", "hi")
        assert "What's your name" in result
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_exception_rolls_back_and_hides_details(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        ctx.deps.whatsapp_client.send_text.side_effect = RuntimeError(SECRET)
        result = await send_group_message(ctx, "Book Club", "hi")
        assert "hunter2" not in result
        ctx.deps.db.rollback.assert_called_once()


class TestTelegramLiveMembershipCheck:
    TG_GROUP = SharedGroup(group_jid="tg:-1001234567890", subject="Team chat")

    def _ctx(self):
        user = make_user("tg:555", name="Ana", phone=None)
        ctx = _make_ctx(whatsapp_jid="tg:555", client_id="telegram", user=user)
        return ctx

    @pytest.fixture(autouse=True)
    def _derived(self):
        with patch(
            "ai_api.services.shared_groups.derive_telegram_shared_groups",
            return_value=[self.TG_GROUP],
        ):
            yield

    async def test_sends_when_still_a_member(self):
        ctx = self._ctx()
        await send_group_message(ctx, "Team chat", "hi")
        ctx.deps.whatsapp_client.is_group_member.assert_awaited_once_with(
            "tg:-1001234567890", "tg:555"
        )
        ctx.deps.whatsapp_client.send_text.assert_awaited_once()

    async def test_refuses_when_the_user_was_removed(self):
        ctx = self._ctx()
        ctx.deps.whatsapp_client.is_group_member.return_value = False
        result = await send_group_message(ctx, "Team chat", "hi")
        assert "no longer a member" in result
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_fails_closed_when_the_check_errors(self):
        ctx = self._ctx()
        ctx.deps.whatsapp_client.is_group_member.side_effect = RuntimeError("503")
        result = await send_group_message(ctx, "Team chat", "hi")
        assert "didn't send" in result
        ctx.deps.whatsapp_client.send_text.assert_not_called()

    async def test_baileys_path_never_calls_the_telegram_check(self):
        ctx = _make_ctx(shared_groups=[BOOK])
        await send_group_message(ctx, "Book Club", "hi")
        ctx.deps.whatsapp_client.is_group_member.assert_not_called()
