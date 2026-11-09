"""update_core_memory is also the agent's way to forget: the default prompt tells
it to pass an empty string to clear everything (the /memories command covers the
user-driven path, and the current document is always injected into the prompt,
so no separate show/clear tool is needed)."""

from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.tools.memory import is_destructive_rewrite, update_core_memory

MODULE = "ai_api.agent.tools.memory"


def _ctx():
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = "5511999999999@s.whatsapp.net"
    ctx.deps.is_group_admin = None
    return ctx


async def test_empty_content_clears_the_document():
    ctx = _ctx()
    mem = MagicMock(content="## Facts\n- likes tea")
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        result = await update_core_memory(ctx, "")

    assert mem.content == ""
    assert mem.previous_content == "## Facts\n- likes tea"
    ctx.deps.db.commit.assert_called_once()
    assert result == "Core memory saved (0/2000 chars)."


@pytest.mark.parametrize("is_admin", [None, False])
async def test_a_group_member_cannot_clear_the_groups_memory(is_admin):
    ctx = _ctx()
    ctx.deps.whatsapp_jid = "120363012345678@g.us"
    ctx.deps.is_group_admin = is_admin
    mem = MagicMock(content="## Facts")
    with patch(f"{MODULE}.get_or_create_core_memory", return_value=mem):
        result = await update_core_memory(ctx, "  ")
    assert "Only a group admin" in result
    assert mem.content == "## Facts"
    ctx.deps.db.commit.assert_not_called()


async def test_a_group_member_can_still_rewrite_the_memory():
    ctx = _ctx()
    ctx.deps.whatsapp_jid = "tg:-1001234567890"
    mem = MagicMock(content="## Facts")
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        result = await update_core_memory(ctx, "## Facts\n- meets on Fridays")
    assert result == "Core memory saved (27/2000 chars)."


async def test_a_group_admin_can_clear_the_memory():
    ctx = _ctx()
    ctx.deps.whatsapp_jid = "120363012345678@g.us"
    ctx.deps.is_group_admin = True
    mem = MagicMock(content="## Facts")
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        await update_core_memory(ctx, "")
    assert mem.content == ""


async def test_prompt_documents_the_clear_path():
    from ai_api.agent.core import DEFAULT_SYSTEM_PROMPT

    prompt = " ".join(DEFAULT_SYSTEM_PROMPT.split())
    assert "to forget everything, pass an empty string" in prompt


GROUP_DOC = "## Group\n- meets on Fridays at 18h\n- treasurer is Ana\n- dues are R$10"


def _group_ctx(is_admin):
    ctx = _ctx()
    ctx.deps.whatsapp_jid = "120363012345678@g.us"
    ctx.deps.is_group_admin = is_admin
    return ctx


async def _run(ctx, previous, content):
    mem = MagicMock(content=previous, previous_content=None)
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        result = await update_core_memory(ctx, content)
    return mem, result


@pytest.mark.parametrize("is_admin", [None, False])
async def test_a_group_member_cannot_replace_the_memory_with_filler(is_admin):
    ctx = _group_ctx(is_admin)
    mem, result = await _run(ctx, GROUP_DOC, "x")
    assert "Only a group admin" in result
    assert mem.content == GROUP_DOC
    ctx.deps.db.commit.assert_not_called()


async def test_a_group_member_cannot_drop_most_lines():
    ctx = _group_ctx(None)
    mem, result = await _run(ctx, GROUP_DOC, "## Group\n- dues are R$10")
    assert "Only a group admin" in result
    assert mem.content == GROUP_DOC


async def test_a_group_member_can_add_and_edit_a_line():
    ctx = _group_ctx(None)
    new = GROUP_DOC.replace("R$10", "R$12") + "\n- next trip in May"
    mem, result = await _run(ctx, GROUP_DOC, new)
    assert result.startswith("Core memory saved")
    assert mem.content == new
    assert mem.previous_content == GROUP_DOC


async def test_a_group_admin_can_shrink_the_memory():
    ctx = _group_ctx(True)
    mem, result = await _run(ctx, GROUP_DOC, "x")
    assert mem.content == "x"
    assert result == "Core memory saved (1/2000 chars)."


async def test_a_private_user_can_shrink_their_own_memory():
    ctx = _ctx()
    mem, result = await _run(ctx, GROUP_DOC, "x")
    assert mem.content == "x"


async def test_the_reply_never_echoes_the_previous_document():
    ctx = _ctx()
    _, result = await _run(ctx, GROUP_DOC, GROUP_DOC + "\n- new")
    assert "treasurer" not in result


async def test_an_unchanged_document_is_not_rewritten():
    ctx = _ctx()
    mem, _ = await _run(ctx, GROUP_DOC, GROUP_DOC)
    assert mem.previous_content is None
    ctx.deps.db.commit.assert_not_called()


@pytest.mark.parametrize(
    ("previous", "new", "expected"),
    [
        ("", "", False),
        ("", "anything", False),
        ("- a fact", "", True),
        ("- a fact", "   ", True),
        (GROUP_DOC, GROUP_DOC, False),
        (GROUP_DOC, GROUP_DOC + "\n- more", False),
        (GROUP_DOC, "x" * len(GROUP_DOC), True),
        # Reordering and re-indenting keep every line.
        (GROUP_DOC, "\n".join(reversed(GROUP_DOC.splitlines())), False),
        (GROUP_DOC, "  " + GROUP_DOC.replace("\n", "\n  "), False),
        # Exactly half of the characters kept is not destructive.
        ("aaaa\nbbbb", "aaaa", False),
        ("aaaa\nbbbbb", "aaaa", True),
    ],
)
def test_is_destructive_rewrite(previous, new, expected):
    assert is_destructive_rewrite(previous, new) is expected
