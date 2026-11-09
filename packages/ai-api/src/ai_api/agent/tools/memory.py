from pydantic_ai import RunContext

from ...database import get_or_create_core_memory, is_group_jid
from ...logger import logger
from ...runtime_config import runtime_config
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal

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


def backup_after_save(previous: str, backup: str | None, *, trusted: bool) -> str | None:
    """The ``/memories undo`` backup to keep when ``previous`` is replaced.

    One level, but a backup worth keeping is never lost to a save:
      - an empty ``previous`` never replaces a backup (after a mistaken
        ``/memories clear``, the next save must not overwrite the cleared text);
      - an untrusted save (a group member who isn't a confirmed admin) moves
        the backup only when it is empty, so it keeps the last state from
        before the members' edits. However a member erodes the document, one
        cut at a time or by appending filler then cutting to it, an admin's
        ``/memories undo`` restores that state.
    A trusted save (a private chat, or a confirmed group admin) keeps the
    document it replaced. Pure, so an operation-based memory tool can reuse it.
    """
    if not previous.strip():
        return backup
    if trusted or not (backup or "").strip():
        return previous
    return backup


@agent.tool
async def update_core_memory(ctx: RunContext[AgentDeps], content: str) -> str:
    """
    Rewrite your persistent notes about this user.

    This REPLACES the entire core memory document with the new content.
    Anything not included in the new content will be lost. Your current
    core memory is shown in the system prompt — use it as the base when
    adding or modifying notes.

    Use markdown formatting for organization (headings, bullets, etc.).
    Keep it concise — max ~2000 characters.

    Args:
        ctx: Run context with database and user info
        content: The full new markdown content for the core memory document.
            Must include ALL information you want to preserve.

    Returns:
        A short confirmation with the new size and the limit
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
            # Same rule as group_admin_refusal: a private chat, or a group
            # admin confirmed by the client (anything but True is not).
            trusted = not is_group_jid(ctx.deps.whatsapp_jid) or ctx.deps.is_group_admin is True
            mem.previous_content = backup_after_save(
                previous, mem.previous_content, trusted=trusted
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
