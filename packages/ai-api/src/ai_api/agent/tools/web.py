import asyncio

import httpx
from ddgs import DDGS
from pydantic_ai import RunContext

from ...config import settings
from ...logger import logger
from ..core import AgentDeps, agent


@agent.tool
async def web_search(ctx: RunContext[AgentDeps], query: str) -> str:
    """
    Search the web for current information about a topic.

    Use this tool when you need up-to-date information that may not be in your
    training data, such as recent news, current events, real-time data, or
    the latest documentation.

    Do NOT use this for:
    - Historical facts or general knowledge (use your training)
    - Information about past conversations (use search_conversation_history)
    - Content from uploaded documents (use search_knowledge_base)

    Args:
        ctx: Run context (unused but required by decorator)
        query: The search query - be specific for better results

    Returns:
        Formatted search results with titles, snippets, and source URLs
    """
    logger.info("=" * 80)
    logger.info("🌐 TOOL CALLED: web_search")
    logger.info(f"   Query: '{query}'")
    logger.info("=" * 80)

    try:
        # DDGS is sync-only, run in executor to not block event loop
        def _search():
            with DDGS() as ddgs:
                return ddgs.text(query, max_results=10)

        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, _search)

        if not results:
            logger.info("No search results found")
            return f"No results found for: {query}"

        formatted = []
        for r in results:
            formatted.append(f"**{r['title']}**\n{r['body']}\nSource: {r['href']}")

        result_text = "\n\n".join(formatted)

        logger.info(f"Found {len(results)} search results")
        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: web_search")
        logger.info(f"   Returning {len(result_text)} characters to agent")
        logger.info("=" * 80)

        return result_text

    except Exception as e:
        logger.error(f"Web search failed: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: web_search")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Search failed: {str(e)}"


@agent.tool
async def fetch_website(ctx: RunContext[AgentDeps], url: str) -> str:
    """
    Fetch and read the content of a specific website URL.

    Use this tool when the user shares a link or asks you to read, summarize,
    or analyze the content of a specific webpage.

    Do NOT use this for:
    - Searching the web (use web_search instead)
    - Finding information without a specific URL

    Args:
        ctx: Run context with HTTP client
        url: The full URL to fetch (must start with http:// or https://)

    Returns:
        Page content as clean markdown, or error message
    """
    logger.info("=" * 80)
    logger.info("📄 TOOL CALLED: fetch_website")
    logger.info(f"   URL: '{url}'")
    logger.info("=" * 80)

    if not ctx.deps.http_client:
        return "HTTP client not available."

    if not url.startswith(("http://", "https://")):
        return "Invalid URL. Must start with http:// or https://"

    try:
        # Use Jina Reader API to get clean markdown
        jina_url = f"https://r.jina.ai/{url}"
        headers = {}
        if settings.jina_api_key:
            headers["Authorization"] = f"Bearer {settings.jina_api_key}"
        response = await ctx.deps.http_client.get(jina_url, headers=headers, timeout=30.0)
        response.raise_for_status()

        content = response.text

        # Truncate if too long (keep first ~8000 chars for LLM context)
        if len(content) > 8000:
            content = content[:8000] + "\n\n[Content truncated...]"

        logger.info(f"Fetched {len(content)} characters from URL")
        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: fetch_website")
        logger.info(f"   Returning {len(content)} characters to agent")
        logger.info("=" * 80)

        return content

    except httpx.TimeoutException:
        error_msg = f"Timeout fetching URL: {url}"
        logger.error(error_msg)
        return error_msg
    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP error {e.response.status_code} fetching URL: {url}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        logger.error(f"Failed to fetch URL: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: fetch_website")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to fetch URL: {str(e)}"
