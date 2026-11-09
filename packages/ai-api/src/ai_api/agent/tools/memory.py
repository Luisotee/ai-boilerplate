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


@agent.tool
async def update_core_memory(ctx: RunContext[AgentDeps], content: str) -> str:
    """
    Rewrite your persistent notes about this user.

    This REPLACES the entire core memory document with the new content.
    Anything not included in the new content will be lost. Your current
    core memory is shown in the system prompt — use it as the base when
    adding or modifying notes.

    Save lasting facts the user shares (name, place, work, family, preferences
    about how you answer) without announcing it unless they asked you to
    remember. Skip transient or trivial things. Short bullets, max ~2000 characters.
    To forget everything, pass an empty string (in a group only an admin may clear it).

    Args:
        ctx: Run context with database and user info
        content: The full new markdown content for the core memory document.
            Must include ALL information you want to preserve.
    """
    logger.info("=" * 80)
    logger.info("TOOL CALLED: update_core_memory")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info(f"   Content length: {len(content)} characters")
    logger.info("=" * 80)

    try:
        max_length = runtime_config.get("core_memory_max_length")
        if len(content) > max_length:
            return (
                f"Content too long ({len(content)} characters). "
                f"Maximum is {max_length} characters. Please shorten your notes."
            )

        mem = get_or_create_core_memory(ctx.deps.db, ctx.deps.user_id)
        previous = mem.content or ""

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

        logger.info("TOOL RETURNING: update_core_memory")
        logger.info(f"   Saved {len(content)} characters")
        logger.info("=" * 80)

        # Never echo the old document back: it costs up to max_length tokens on
        # every save, and the current one is already in the instructions.
        return f"Core memory saved ({len(content)}/{max_length} chars)."

    except Exception as e:
        safe_rollback(ctx.deps.db)
        logger.error(f"Error updating core memory: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("TOOL ERROR: update_core_memory")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return "Failed to update core memory. Please try again."
