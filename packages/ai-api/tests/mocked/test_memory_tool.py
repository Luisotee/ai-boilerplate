"""The operation-based memory tool (agent/tools/memory.py).

The agent edits its notes with add / patch / remove operations instead of
re-sending the whole document, so it can't drop neighbouring facts by
accident. The group guard (is_destructive_rewrite + group_admin_refusal) and
the /memories undo backup (backup_after_save) apply to the before/after
documents exactly as they did for the whole-document rewrite.
"""

import re
from unittest.mock import MagicMock, patch

import pytest

from ai_api.agent.tools.memory import (
    MemoryOperation,
    MemoryOperationError,
    apply_operations,
    backup_after_save,
    is_destructive_rewrite,
    memory,
)
from ai_api.commands import _handle_memories_command

MODULE = "ai_api.agent.tools.memory"


def _ops(previous: str, content: str) -> list[MemoryOperation]:
    """The remove/add operations that turn ``previous`` into ``content``.

    Lets the guard and backup tests below state a before/after document, as
    they did for the whole-document tool, through the real operations path.
    """
    old = [line for line in previous.splitlines() if line.strip()]
    new = [line for line in content.splitlines() if line.strip()]
    ops = [MemoryOperation(action="remove", old_text=line) for line in old if line not in new]
    ops += [MemoryOperation(action="add", text=line) for line in new if line not in old]
    return ops


def _ctx():
    ctx = MagicMock()
    ctx.deps.db = MagicMock()
    ctx.deps.user_id = "user-123"
    ctx.deps.whatsapp_jid = "5511999999999@s.whatsapp.net"
    ctx.deps.is_group_admin = None
    return ctx


async def test_removing_every_line_clears_the_document():
    ctx = _ctx()
    mem = MagicMock(content="## Facts\n- likes tea")
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        result = await memory(ctx, _ops(mem.content, ""))

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
        result = await memory(ctx, _ops(mem.content, "  "))
    assert "Only a group admin" in result
    assert mem.content == "## Facts"
    ctx.deps.db.commit.assert_not_called()


async def test_a_group_member_can_still_add_to_the_memory():
    ctx = _ctx()
    ctx.deps.whatsapp_jid = "tg:-1001234567890"
    mem = MagicMock(content="## Facts")
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        result = await memory(ctx, _ops(mem.content, "## Facts\n- meets on Fridays"))
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
        await memory(ctx, _ops(mem.content, ""))
    assert mem.content == ""


async def test_tool_documents_the_clear_path():
    from ai_api.agent.core import agent

    description = agent._function_toolset.tools["memory"].tool_def.description
    assert "To forget everything, remove every line" in " ".join(description.split())
    assert "Remember the preference, not the instance" in " ".join(description.split())


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
        result = await memory(ctx, _ops(mem.content, content))
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


async def _save(ctx, mem, content):
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        return await memory(ctx, _ops(mem.content, content))


async def test_two_step_erosion_by_a_member_leaves_the_original_as_the_backup():
    # Append filler (every old line kept), then cut down to the filler: each
    # step passes the per-save guard, but the backup keeps the pre-member
    # document, so an admin's /memories undo brings it back.
    ctx = _group_ctx(None)
    filler = "\n".join(f"- filler line {i:03d} padding padding" for i in range(30))
    mem = MagicMock(content=GROUP_DOC, previous_content=None)
    assert (await _save(ctx, mem, GROUP_DOC + "\n" + filler)).startswith("Core memory saved")
    assert (await _save(ctx, mem, filler)).startswith("Core memory saved")
    assert mem.content == filler
    assert mem.previous_content == GROUP_DOC


async def test_a_save_after_a_clear_keeps_the_cleared_document_as_the_backup():
    # /memories clear left content "" and the backup X; a member's next
    # message makes the agent save "Y". The backup must still be X.
    ctx = _group_ctx(None)
    mem = MagicMock(content="", previous_content=GROUP_DOC)
    await _save(ctx, mem, "## Group\n- new fact")
    assert mem.content == "## Group\n- new fact"
    assert mem.previous_content == GROUP_DOC


async def test_an_admin_save_moves_the_backup():
    ctx = _group_ctx(True)
    mem = MagicMock(content=GROUP_DOC + "\n- a", previous_content=GROUP_DOC)
    await _save(ctx, mem, GROUP_DOC + "\n- b")
    assert mem.previous_content == GROUP_DOC + "\n- a"


def _mem(content, backup=None, trusted=True):
    return MagicMock(content=content, previous_content=backup, content_trusted=trusted)


def _undo(mem):
    # What /memories undo does (commands.py): swap, and an admin chose it.
    with patch("ai_api.commands.get_or_create_core_memory", return_value=mem):
        _handle_memories_command(MagicMock(), "user-123", ["/memories", "undo"])


async def test_undo_after_erosion_restores_the_admins_latest_save_not_an_older_one():
    # Admin X -> G, members add a fact, a member erodes: undo must give G.
    admin, member = _group_ctx(True), _group_ctx(None)
    g = GROUP_DOC + "\n- admin note"
    mem = _mem("## Group\n- old")
    await _save(admin, mem, g)
    assert mem.content_trusted is True
    await _save(member, mem, g + "\n- member fact")
    assert mem.content_trusted is False
    filler = "\n".join(f"- filler line {i:03d} padding padding" for i in range(30))
    await _save(member, mem, g + "\n- member fact\n" + filler)
    await _save(member, mem, filler)
    _undo(mem)
    assert mem.content == g
    assert mem.content_trusted is True


async def test_a_second_erosion_after_an_undo_still_restores_the_admin_state():
    # Member erodes G -> E, admin undoes (backup becomes E), a member adds and
    # erodes again: the next undo must give G, not the rejected E.
    member = _group_ctx(None)
    mem = _mem(GROUP_DOC)
    filler = "\n".join(f"- filler line {i:03d} padding padding" for i in range(30))
    await _save(member, mem, GROUP_DOC + "\n" + filler)
    await _save(member, mem, filler)
    _undo(mem)
    assert mem.content == GROUP_DOC
    await _save(member, mem, GROUP_DOC + "\n- new fact")
    await _save(member, mem, GROUP_DOC + "\n- new fact\n" + filler)
    await _save(member, mem, filler)
    _undo(mem)
    assert mem.content == GROUP_DOC


@pytest.mark.parametrize(
    ("previous", "backup", "trusted", "previous_trusted", "expected"),
    [
        ("A", None, False, False, ("A", False)),  # no backup yet: take it
        ("A", "", False, False, ("A", False)),
        ("B", "A", False, False, ("A", False)),  # untrusted B is never backed up
        ("B", "A", False, True, ("B", False)),  # trusted B is, even by a member
        ("B", "A", True, False, ("B", True)),  # a trusted save backs up what it replaces
        ("", "A", True, True, ("A", True)),  # an empty document never replaces it
        ("  ", "A", False, True, ("A", False)),
        ("", None, True, True, (None, True)),
    ],
)
def test_backup_after_save(previous, backup, trusted, previous_trusted, expected):
    result = backup_after_save(previous, backup, trusted=trusted, previous_trusted=previous_trusted)
    assert result == expected


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


# --- operations ---------------------------------------------------------------


def _op(action, text="", old_text=""):
    return MemoryOperation(action=action, text=text, old_text=old_text)


DOC = "- name: Ana\n- lives in Belém\n- likes tea\n- likes coffee"


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        ([_op("add", "- has a dog")], DOC + "\n- has a dog"),
        ([_op("add", "  - trailing spaces  ")], DOC + "\n- trailing spaces"),
        ([_op("patch", "Santarém", "Belém")], DOC.replace("Belém", "Santarém")),
        ([_op("remove", old_text="tea")], "- name: Ana\n- lives in Belém\n- likes coffee"),
        # In order: a later op sees the earlier ones' result.
        (
            [_op("add", "- likes mate"), _op("remove", old_text="mate")],
            DOC,
        ),
        # A patch to "" removes the span; blank lines left behind are tidied.
        ([_op("patch", "", "- likes tea\n")], "- name: Ana\n- lives in Belém\n- likes coffee"),
    ],
)
def test_apply_operations(ops, expected):
    assert apply_operations(DOC, ops) == expected


def test_add_to_an_empty_document():
    assert apply_operations("", [_op("add", "- first fact")]) == "- first fact"


@pytest.mark.parametrize(
    ("op", "message"),
    [
        (_op("add", "   "), "operation 1 (add): text is empty"),
        (_op("patch", "x"), "operation 1 (patch): old_text is empty"),
        (_op("patch", "x", "Recife"), "was not found"),
        (_op("patch", "x", "likes"), "matches 2 places"),
        (_op("remove", old_text="likes"), "matches 2 lines"),
        (_op("remove", old_text="Recife"), "was not found"),
    ],
)
def test_an_operation_that_cannot_apply_is_named(op, message):
    with pytest.raises(MemoryOperationError, match=re.escape(message)):
        apply_operations(DOC, [op])


async def _call(ctx, previous, ops, max_length=2000):
    mem = MagicMock(content=previous, previous_content=None, content_trusted=True)
    with (
        patch(f"{MODULE}.get_or_create_core_memory", return_value=mem),
        patch(f"{MODULE}.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = max_length
        result = await memory(ctx, ops)
    return mem, result


async def test_a_batch_is_all_or_nothing():
    ctx = _ctx()
    mem, result = await _call(ctx, DOC, [_op("add", "- has a dog"), _op("remove", old_text="x")])
    assert result.startswith("Nothing saved: operation 2 (remove)")
    assert mem.content == DOC
    ctx.deps.db.commit.assert_not_called()


async def test_over_the_limit_nothing_is_saved_and_the_model_is_told_to_make_room():
    ctx = _ctx()
    mem, result = await _call(ctx, DOC, [_op("add", "- " + "x" * 50)], max_length=len(DOC) + 10)
    assert "Nothing saved: the notes would be" in result
    assert "make room" in result
    assert mem.content == DOC


async def test_a_patch_saves_and_reports_the_size():
    ctx = _ctx()
    mem, result = await _call(ctx, DOC, [_op("patch", "Santarém", "Belém")])
    assert mem.content == DOC.replace("Belém", "Santarém")
    assert mem.previous_content == DOC
    assert result == f"Core memory saved ({len(mem.content)}/2000 chars)."


async def test_no_operations_saves_nothing():
    ctx = _ctx()
    mem, result = await _call(ctx, DOC, [])
    assert "nothing saved" in result
    ctx.deps.db.commit.assert_not_called()


async def test_core_memory_header_shows_the_size():
    from ai_api.agent.core import inject_core_memory

    ctx = MagicMock()
    with (
        patch("ai_api.agent.core.get_or_create_core_memory", return_value=MagicMock(content=DOC)),
        patch("ai_api.agent.core.runtime_config") as mock_rc,
    ):
        mock_rc.get.return_value = 2000
        text = await inject_core_memory(ctx)
    assert f"{len(DOC)}/2000 chars" in text
    assert DOC in text


@pytest.mark.parametrize("old_text", ["- likes tea\n", "- likes tea\r\n", "\n- likes tea"])
def test_remove_matches_a_line_copied_with_its_line_break(old_text):
    assert "- likes tea" not in apply_operations(DOC, [_op("remove", old_text=old_text)])


async def test_a_shrinking_edit_saves_when_the_limit_was_lowered_below_the_notes():
    previous = "\n".join(f"- fact number {i:03d}" for i in range(100))  # ~1.9k chars
    mem, result = await _call(
        _ctx(), previous, [_op("remove", old_text="fact number 000")], max_length=1000
    )
    assert "fact number 000" not in mem.content
    assert result.startswith("Core memory saved")


async def test_an_edit_that_grows_past_the_limit_is_refused():
    mem, result = await _call(_ctx(), "- a", [_op("add", "- " + "x" * 50)], max_length=20)
    assert mem.content == "- a"
    assert result.startswith("Nothing saved")


async def test_tidying_a_legacy_document_alone_is_not_a_change():
    ctx = _ctx()
    legacy = "- likes tea   \n\n\n\n- likes coffee"
    # Patch a span to itself: the result differs from `legacy` only by tidying.
    mem, _ = await _call(ctx, legacy, [_op("patch", "likes tea", "likes tea")])
    assert mem.previous_content is None
    ctx.deps.db.commit.assert_not_called()
