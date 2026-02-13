from pydantic_ai import RunContext

from ...config import settings
from ...database import get_or_create_core_memory
from ...logger import logger
from ..core import AgentDeps, agent


@agent.tool
async def get_core_memory(ctx: RunContext[AgentDeps]) -> str:
    """
    Read your persistent notes about this user.

    Returns the full core memory document (markdown). Use this before updating
    to ensure you have the latest version and don't accidentally lose information.

    The core memory is also shown in your system prompt at the start of each
    conversation, but this tool gives you the latest state mid-conversation.

    Args:
        ctx: Run context with database and user info

    Returns:
        The core memory markdown content, or a message if empty
    """
    logger.info("=" * 80)
    logger.info("TOOL CALLED: get_core_memory")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info("=" * 80)

    try:
        mem = get_or_create_core_memory(ctx.deps.db, ctx.deps.user_id)

        if not mem.content:
            result = "No core memory saved yet. Use update_core_memory to create one."
        else:
            result = mem.content

        logger.info("TOOL RETURNING: get_core_memory")
        logger.info(f"   Content length: {len(result)} characters")
        logger.info("=" * 80)

        return result

    except Exception as e:
        logger.error(f"Error reading core memory: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("TOOL ERROR: get_core_memory")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to read core memory: {str(e)}"


@agent.tool
async def update_core_memory(ctx: RunContext[AgentDeps], content: str) -> str:
    """
    Rewrite your persistent notes about this user.

    This REPLACES the entire core memory document with the new content.
    Anything not included in the new content will be lost. Always read
    the current memory first (from system prompt or get_core_memory) and
    include all information you want to keep.

    Use markdown formatting for organization (headings, bullets, etc.).
    Keep it concise — max ~2000 characters.

    Args:
        ctx: Run context with database and user info
        content: The full new markdown content for the core memory document.
            Must include ALL information you want to preserve.

    Returns:
        Confirmation or error message
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
        mem.content = content
        ctx.deps.db.commit()

        logger.info("TOOL RETURNING: update_core_memory")
        logger.info(f"   Saved {len(content)} characters")
        logger.info("=" * 80)

        return f"Core memory updated ({len(content)} characters)."

    except Exception as e:
        logger.error(f"Error updating core memory: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("TOOL ERROR: update_core_memory")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to update core memory: {str(e)}"
