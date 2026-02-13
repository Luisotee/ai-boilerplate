from pydantic_ai import RunContext

from ...config import settings
from ...database import get_or_create_core_memory
from ...logger import logger
from ..core import AgentDeps, agent


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
        Confirmation with previous content for verification
    """
    logger.info("=" * 80)
    logger.info("TOOL CALLED: update_core_memory")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info(f"   Content length: {len(content)} characters")
    logger.info("=" * 80)

    try:
        max_length = settings.core_memory_max_length
        if len(content) > max_length:
            return (
                f"Content too long ({len(content)} characters). "
                f"Maximum is {max_length} characters. Please shorten your notes."
            )

        mem = get_or_create_core_memory(ctx.deps.db, ctx.deps.user_id)
        previous = mem.content
        mem.content = content
        ctx.deps.db.commit()

        logger.info("TOOL RETURNING: update_core_memory")
        logger.info(f"   Saved {len(content)} characters")
        logger.info("=" * 80)

        if previous:
            return (
                f"Core memory updated ({len(content)} characters). "
                f"Previous content was:\n{previous}"
            )
        return f"Core memory updated ({len(content)} characters)."

    except Exception as e:
        logger.error(f"Error updating core memory: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("TOOL ERROR: update_core_memory")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to update core memory: {str(e)}"
