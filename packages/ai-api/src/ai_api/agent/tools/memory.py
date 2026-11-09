import re
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import RunContext

from ...database import get_or_create_core_memory
from ...logger import logger
from ...runtime_config import runtime_config
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal, is_trusted_sender

# A group rewrite by a non-admin is refused when less than this share of the
# previous document survives it (see is_destructive_rewrite).
MIN_KEPT_SHARE = 0.5


def is_destructive_rewrite(previous: str, new: str) -> bool:
    """Whether replacing ``previous`` with ``new`` throws away most of it.

    True for an empty ``new`` (a wipe), or when the previous document's
    non-blank lines that survive VERBATIM (after stripping) in ``new`` hold
    less than ``MIN_KEPT_SHARE`` of its characters. Lines, not length, so a
    rewrite to filler of the same size still counts as destructive. Pure and
    self-contained so an operation-based memory tool can call it on the
    before/after documents too.
    """
    if not new.strip():
        return bool(previous.strip())
    previous_lines = [line.strip() for line in previous.splitlines() if line.strip()]
    total = sum(len(line) for line in previous_lines)
    if total == 0:
        return False
    new_lines = {line.strip() for line in new.splitlines() if line.strip()}
    kept = sum(len(line) for line in previous_lines if line in new_lines)
    return kept / total < MIN_KEPT_SHARE


def backup_after_save(
    previous: str,
    backup: str | None,
    *,
    trusted: bool,
    previous_trusted: bool,
) -> tuple[str | None, bool]:
    """The ``/memories undo`` backup and trust flag after ``previous`` is replaced.

    ``trusted``: whether this save comes from a private chat or a confirmed
    group admin. ``previous_trusted``: whether ``previous`` was written by one
    (or chosen by ``/memories clear`` / undo). Returns ``(backup, new flag)``.

      - An empty ``previous`` never replaces a backup, so the next save after a
        mistaken ``/memories clear`` can't overwrite the cleared text.
      - A trusted save backs up the document it replaces.
      - An untrusted save (a group member who isn't a confirmed admin) backs up
        ``previous`` only when it is trusted (or there is no backup yet), so the
        backup is always the last trusted state. However members erode the
        document afterwards (cut by cut, or filler appended then everything
        else cut), an admin's ``/memories undo`` restores that state, which
        drops the members' edits since.
    Pure, so an operation-based memory tool can reuse it.
    """
    if previous.strip() and (trusted or previous_trusted or not (backup or "").strip()):
        backup = previous
    return backup, trusted


class MemoryOperation(BaseModel):
    """One edit to the notes: add a line, patch a span, or remove a line."""

    action: Literal["add", "patch", "remove"]
    text: str = ""
    old_text: str = ""


class MemoryOperationError(ValueError):
    """An operation that can't be applied; the message is shown to the model."""


def apply_operations(document: str, operations: list[MemoryOperation]) -> str:
    """Apply ``operations`` in order to ``document`` and return the result.

    - add: append ``text`` (one or more lines) at the end;
    - patch: replace ``old_text``, which must occur exactly once, with ``text``;
    - remove: delete the one line that contains ``old_text``.
    Raises ``MemoryOperationError`` naming the failed operation; the caller
    saves nothing then, so a batch is all or nothing. Pure.
    """
    for number, op in enumerate(operations, start=1):
        where = f"operation {number} ({op.action})"
        if op.action == "add":
            text = op.text.strip()
            if not text:
                raise MemoryOperationError(f"{where}: text is empty")
            document = f"{document.rstrip()}\n{text}" if document.strip() else text
        elif not op.old_text.strip():
            raise MemoryOperationError(f"{where}: old_text is empty")
        elif op.action == "patch":
            count = document.count(op.old_text)
            if count != 1:
                raise MemoryOperationError(f"{where}: {_match_problem(count, 'places')}")
            document = document.replace(op.old_text, op.text, 1)
        else:  # remove
            lines = document.splitlines()
            hits = [i for i, line in enumerate(lines) if op.old_text in line]
            if len(hits) != 1:
                raise MemoryOperationError(f"{where}: {_match_problem(len(hits), 'lines')}")
            del lines[hits[0]]
            document = "\n".join(lines)
    return _tidy(document)


def _match_problem(count: int, unit: str) -> str:
    if count == 0:
        return "old_text was not found; copy it exactly from your notes"
    return f"old_text matches {count} {unit}; include more of the line so it matches one"


def _tidy(document: str) -> str:
    """Strip trailing spaces, collapse runs of blank lines, trim the ends."""
    lines = [line.rstrip() for line in document.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@agent.tool
async def memory(ctx: RunContext[AgentDeps], operations: list[MemoryOperation]) -> str:
    """
    Edit your persistent notes about this chat (shown under CORE MEMORY, with their size).

    Operations apply in order, all or none:
    - add: append text as a new line, e.g. "- allergic to shrimp".
    - patch: replace old_text (exact, found once in the notes) with text; use it to
      update a fact in place instead of adding a second version.
    - remove: delete the line containing old_text.

    Keep lasting facts: name, place, work, family, how they want you to answer.
    Skip one-off requests, task progress, temporary state, secrets, and what the chat
    history can tell you again. Remember the preference, not the instance ("prefers
    short answers", not "asked for a short answer today"). One fact per short line,
    no headings. Near the size limit, merge or drop old lines in the same call.
    Don't announce saving unless they asked you to remember. In a group the notes are
    the group's. To forget everything, remove every line (in a group only an admin
    may); the user can also send /memories clear.

    Args:
        operations: The edits, each with action and text and/or old_text
    """
    actions = ",".join(op.action for op in operations)
    logger.info(f"TOOL CALLED: memory user={ctx.deps.user_id} operations=[{actions}]")

    if not operations:
        return "No operations given; nothing saved."

    try:
        max_length = runtime_config.get("core_memory_max_length")
        mem = get_or_create_core_memory(ctx.deps.db, ctx.deps.user_id)
        previous = mem.content or ""

        try:
            content = apply_operations(previous, operations)
        except MemoryOperationError as e:
            return f"Nothing saved: {e}."

        if len(content) > max_length:
            return (
                f"Nothing saved: the notes would be {len(content)}/{max_length} chars. "
                "In the same call, patch or remove lines to make room (merge related "
                "facts, drop outdated ones), then add."
            )

        # In a group, dropping most of the document (a wipe included) is what
        # /memories clear reserves for admins; additive and small edits stay
        # open to every member. Fails closed (group_admin_refusal).
        if is_destructive_rewrite(previous, content):
            refusal = group_admin_refusal(
                ctx.deps, "remove most of this group's memory", "/memories clear"
            )
            if refusal:
                return refusal

        if content != previous:
            mem.previous_content, mem.content_trusted = backup_after_save(
                previous,
                mem.previous_content,
                trusted=is_trusted_sender(ctx.deps),
                previous_trusted=mem.content_trusted is not False,
            )
            mem.content = content
            ctx.deps.db.commit()

        logger.info(f"memory saved: {len(content)}/{max_length} chars")
        # Never echo the document: it is already in the instructions.
        return f"Core memory saved ({len(content)}/{max_length} chars)."

    except Exception as e:
        safe_rollback(ctx.deps.db)
        logger.error(f"Error updating core memory: {str(e)}", exc_info=True)
        return "Failed to update core memory. Please try again."
