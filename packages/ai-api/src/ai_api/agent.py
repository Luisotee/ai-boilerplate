import asyncio
import base64
from dataclasses import dataclass

import httpx
import pint
import wikipediaapi
from ddgs import DDGS
from pydantic_ai import Agent, BinaryContent, RunContext
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from simpleeval import simple_eval
from sqlalchemy.orm import Session

from .config import settings
from .embeddings import EmbeddingService
from .logger import logger
from .rag.conversation import (
    format_conversation_results,
)
from .rag.conversation import (
    search_conversation_history as search_conversation_fn,
)
from .rag.knowledge_base import (
    format_knowledge_base_results,
)
from .rag.knowledge_base import (
    search_knowledge_base as search_kb_fn,
)
from .whatsapp import WhatsAppClient

# Unit registry for conversions (created once at module load)
ureg = pint.UnitRegistry()


@dataclass
class AgentDeps:
    """
    Dependencies for agent tools.

    Follows Pydantic AI best practices by injecting all dependencies
    via this dataclass instead of using global singletons.
    """

    db: Session
    user_id: str
    whatsapp_jid: str
    recent_message_ids: list[str]
    embedding_service: EmbeddingService | None = None
    http_client: httpx.AsyncClient | None = None
    whatsapp_client: WhatsAppClient | None = None
    current_message_id: str | None = None


# Create Google provider and model with API key from settings
google_provider = GoogleProvider(api_key=settings.gemini_api_key)
google_model = GoogleModel("gemini-2.5-flash", provider=google_provider)

# Create the AI agent with dependencies
agent = Agent(
    model=google_model,
    deps_type=AgentDeps,
    retries=3,  # Increase from default 1 to handle occasional malformed Gemini responses
    system_prompt="""You are a helpful AI assistant communicating via WhatsApp.
    Be concise, friendly, and helpful. Keep responses brief and to the point.
    If you don't know something, say so clearly.

    You have access to search tools, web tools, and WhatsApp action tools:

    **Search Tools:**
    1. **search_conversation_history** - Searches past messages with this user
       Use when user asks about previous conversations or references past topics

    2. **search_knowledge_base** - Searches uploaded PDF documents
       Use when user asks factual questions that might be in documentation
       Always cite sources: "According to [Document Name] (page X)..."

    **Web Tools:**
    3. **web_search** - Search the internet for current information
       Use for: recent news, current events, up-to-date facts, latest documentation
       Do NOT use for: historical facts, general knowledge in your training

    4. **fetch_website** - Read content from a specific URL
       Use for: when user shares a link, asks to summarize/analyze a webpage
       Do NOT use for: searching (use web_search instead)

    **WhatsApp Action Tools:**
    5. **send_whatsapp_reaction** - React to the user's message with an emoji
       Use when the message warrants an emotional response or acknowledgment
       Common: 👍 (approval), ❤️ (love/thanks), 😂 (funny), 😮 (surprised)

    6. **send_whatsapp_location** - Send a location with coordinates
       Use when sharing a place would be helpful (directions, recommendations)

    7. **send_whatsapp_contact** - Send a contact card
       Use when sharing contact information (support numbers, business contacts)

    8. **send_whatsapp_message** - Send an additional text message
       Use sparingly - only for follow-up messages separate from your main response

    **Utility Tools:**
    9. **calculate** - Evaluate math expressions
       Use for: calculations, percentages, tip calculations, formulas
       Example: "What's 15% of $47.80?" → calculate("47.80 * 0.15")

    10. **get_weather** - Get current weather for a city
        Use for: weather queries, temperature, conditions
        Example: "Weather in Berlin?" → get_weather("Berlin")

    11. **wikipedia_lookup** - Look up factual information on Wikipedia
        Use for: definitions, facts, biographies, historical info
        Do NOT use for: current events (use web_search instead)

    12. **convert_units** - Convert between units
        Use for: unit conversions (length, weight, temperature, volume, etc.)
        Example: "100 km to miles" → convert_units(100, "km", "miles")

    **When NOT to use tools:**
    - Simple greetings or chitchat (no tools needed)
    - Questions fully answerable with recent context (no search needed)
    - General knowledge queries (use your training)

    **Important:** WhatsApp tools only send to the current conversation. You cannot message other users.

    When citing knowledge base sources, ALWAYS include document name, page number, and section heading.""",
)


@agent.tool
async def search_conversation_history(ctx: RunContext[AgentDeps], search_query: str) -> str:
    """
    Search through conversation history for messages related to a specific topic.

    Use this tool when the user asks about past conversations or when you need
    context from older messages that aren't in the recent history.

    Args:
        ctx: Run context with database and user info
        search_query: The topic or question to search for in past messages

    Returns:
        Formatted string with relevant past messages or error message
    """
    logger.info("=" * 80)
    logger.info("🔍 TOOL CALLED: search_conversation_history")
    logger.info(f"   Query: '{search_query}'")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info("=" * 80)

    deps = ctx.deps

    # Check if semantic search dependencies are available
    if not deps.embedding_service:
        return (
            "Semantic search is not available (GEMINI_API_KEY not configured). "
            "I can only access recent messages."
        )

    # Perform semantic search
    try:
        # Generate query embedding using injected service
        query_embedding = await deps.embedding_service.generate(
            search_query,
            task_type="RETRIEVAL_QUERY",  # Different task type for queries
        )

        if not query_embedding:
            return "Failed to generate search embedding. Please try again."

        # Call pure function for semantic search (uses env defaults)
        messages = await search_conversation_fn(
            db=deps.db,
            query_embedding=query_embedding,
            user_id=deps.user_id,
            query_text=search_query,  # For logging/debugging
            exclude_message_ids=deps.recent_message_ids,
            # Omit limit, similarity_threshold, context_window to use env defaults
        )

        if not messages:
            logger.info("No relevant past messages found.")
            return (
                f"No relevant past messages found for: {search_query}. "
                "Either we haven't discussed this topic, or messages are too old/dissimilar."
            )

        logger.info(f"Found {len(messages)} relevant past messages for query: '{search_query}'")

        # Format results with context using pure function
        formatted_results = format_conversation_results(messages)

        # Log detailed results for debugging
        logger.info(f"Conversation RAG returned {len(messages)} results:")
        for i, msg in enumerate(messages, 1):
            logger.info(f"  [{i}] Similarity: {msg.get('similarity_score', 'N/A'):.3f}")
            logger.info(f"      Full content: {msg['matched_message'].content}")

        logger.info(f"Formatted results length: {len(formatted_results)} characters")
        logger.info(f"Full formatted results:\n{formatted_results}")

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: search_conversation_history")
        logger.info(f"   Returning {len(formatted_results)} characters to agent")
        logger.info("=" * 80)

        return formatted_results

    except Exception as e:
        logger.error(f"Error in semantic search: {str(e)}", exc_info=True)
        error_msg = f"Error searching conversation history: {str(e)}"
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: search_conversation_history")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return error_msg


@agent.tool
async def search_knowledge_base(ctx: RunContext[AgentDeps], search_query: str) -> str:
    """
    Search the knowledge base for information from uploaded documents.

    Use this tool when the user asks questions that might be answered by
    documentation, manuals, guides, or other reference materials in the knowledge base.

    DO NOT use this tool for:
    - Questions about past conversations (use search_conversation_history instead)
    - Simple greetings or chitchat
    - Questions that require real-time information

    Args:
        ctx: Run context with database and embedding service
        search_query: The question or topic to search for in documents

    Returns:
        Formatted string with relevant document passages and citations
    """
    logger.info("=" * 80)
    logger.info("📚 TOOL CALLED: search_knowledge_base")
    logger.info(f"   Query: '{search_query}'")
    logger.info("=" * 80)

    deps = ctx.deps

    # Check if knowledge base dependencies are available
    if not deps.embedding_service:
        return (
            "Knowledge base search is not available (GEMINI_API_KEY not configured). "
            "I can only answer based on general knowledge."
        )

    try:
        # Generate query embedding
        query_embedding = await deps.embedding_service.generate(
            search_query,
            task_type="RETRIEVAL_QUERY",  # Different task type for queries
        )

        if not query_embedding:
            return "Failed to generate search embedding. Please try again."

        # Call pure function for knowledge base search (uses env defaults)
        # Pass whatsapp_jid to include conversation-scoped documents
        results = await search_kb_fn(
            db=deps.db,
            query_embedding=query_embedding,
            query_text=search_query,
            whatsapp_jid=deps.whatsapp_jid,
            # Omit limit and similarity_threshold to use env defaults
        )

        if not results:
            logger.info("No relevant documents found in knowledge base")
            return (
                f"No relevant information found in the knowledge base for: {search_query}. "
                "This topic may not be covered in uploaded documents."
            )

        logger.info(f"Found {len(results)} relevant passages from knowledge base")

        # Format results with citations using pure function
        formatted_results = format_knowledge_base_results(results)

        # Log detailed results for debugging
        logger.info(f"Knowledge Base RAG returned {len(results)} results:")
        for i, result in enumerate(results, 1):
            chunk = result["chunk"]
            doc = result["document"]
            similarity = result["similarity_score"]
            logger.info(
                f"  [{i}] Document: {doc['original_filename']} | "
                f"Similarity: {similarity:.3f} | "
                f"Page: {chunk.get('page_number', 'N/A')} | "
                f"Tokens: {chunk.get('token_count', 'N/A')}"
            )
            logger.info(f"      Raw content (before cleaning):\n{chunk['content']}")

        logger.info(f"Formatted results length: {len(formatted_results)} characters")
        logger.info(f"Full formatted results (after cleaning):\n{formatted_results}")

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: search_knowledge_base")
        logger.info(f"   Returning {len(formatted_results)} characters to agent")
        logger.info("=" * 80)

        return formatted_results

    except Exception as e:
        logger.error(f"Error in knowledge base search: {str(e)}", exc_info=True)
        error_msg = f"Error searching knowledge base: {str(e)}"
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: search_knowledge_base")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return error_msg


# =============================================================================
# Web Tools
# =============================================================================


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

        loop = asyncio.get_event_loop()
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


# =============================================================================
# Utility Tools
# =============================================================================


@agent.tool
async def calculate(ctx: RunContext[AgentDeps], expression: str) -> str:
    """
    Evaluate a mathematical expression.

    Use this tool for calculations, percentages, formulas, and basic math.

    Examples:
    - "47.80 * 0.15" for 15% of $47.80
    - "(5 + 3) * 2" for arithmetic
    - "100 / 4" for division

    Args:
        ctx: Run context (unused but required by decorator)
        expression: Math expression to evaluate (e.g., "2 + 2", "15 * 0.15")

    Returns:
        Result of the calculation or error message
    """
    logger.info("=" * 80)
    logger.info("🧮 TOOL CALLED: calculate")
    logger.info(f"   Expression: '{expression}'")
    logger.info("=" * 80)

    try:
        result = simple_eval(expression)

        logger.info(f"Calculation result: {result}")
        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: calculate")
        logger.info(f"   Result: {result}")
        logger.info("=" * 80)

        return f"{expression} = {result}"
    except Exception as e:
        logger.error(f"Calculation failed: {str(e)}")
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: calculate")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Could not calculate: {str(e)}"


@agent.tool
async def get_weather(ctx: RunContext[AgentDeps], city: str) -> str:
    """
    Get current weather for a city.

    Use this tool when the user asks about weather conditions in a specific location.

    Args:
        ctx: Run context with HTTP client
        city: City name (e.g., "Berlin", "New York", "Tokyo")

    Returns:
        Current weather conditions including temperature, wind, humidity
    """
    logger.info("=" * 80)
    logger.info("🌤️ TOOL CALLED: get_weather")
    logger.info(f"   City: '{city}'")
    logger.info("=" * 80)

    if not ctx.deps.http_client:
        return "HTTP client not available."

    try:
        # Step 1: Geocode city name to coordinates
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"
        geo_resp = await ctx.deps.http_client.get(geo_url, timeout=10.0)
        geo_data = geo_resp.json()

        if not geo_data.get("results"):
            logger.info(f"City '{city}' not found")
            return f"City '{city}' not found."

        location = geo_data["results"][0]
        lat, lon = location["latitude"], location["longitude"]
        city_name = location.get("name", city)
        country = location.get("country", "")

        logger.info(f"Geocoded to: {city_name}, {country} ({lat}, {lon})")

        # Step 2: Get current weather
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}&"
            f"current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
        )
        weather_resp = await ctx.deps.http_client.get(weather_url, timeout=10.0)
        weather_data = weather_resp.json()

        current = weather_data["current"]
        temp = current["temperature_2m"]
        humidity = current["relative_humidity_2m"]
        wind = current["wind_speed_10m"]
        code = current["weather_code"]

        # Weather code to description
        conditions = {
            0: "Clear sky",
            1: "Mainly clear",
            2: "Partly cloudy",
            3: "Overcast",
            45: "Foggy",
            48: "Depositing rime fog",
            51: "Light drizzle",
            53: "Moderate drizzle",
            55: "Dense drizzle",
            61: "Slight rain",
            63: "Moderate rain",
            65: "Heavy rain",
            71: "Slight snow",
            73: "Moderate snow",
            75: "Heavy snow",
            80: "Slight rain showers",
            81: "Moderate rain showers",
            82: "Violent rain showers",
            95: "Thunderstorm",
            96: "Thunderstorm with slight hail",
            99: "Thunderstorm with heavy hail",
        }
        condition = conditions.get(code, "Unknown")

        result = (
            f"**{city_name}, {country}**\n"
            f"Temperature: {temp}°C\n"
            f"Wind: {wind} km/h\n"
            f"Humidity: {humidity}%\n"
            f"Conditions: {condition}"
        )

        logger.info(f"Weather retrieved: {temp}°C, {condition}")
        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: get_weather")
        logger.info("=" * 80)

        return result

    except Exception as e:
        logger.error(f"Weather lookup failed: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: get_weather")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Could not get weather: {str(e)}"


@agent.tool
async def wikipedia_lookup(ctx: RunContext[AgentDeps], topic: str) -> str:
    """
    Look up a topic on Wikipedia.

    Use for factual information, definitions, historical facts, and biographies.

    Do NOT use for:
    - Current events or recent news (use web_search instead)
    - Opinions or predictions
    - Real-time information

    Args:
        ctx: Run context (unused but required by decorator)
        topic: The topic to look up (e.g., "Albert Einstein", "Python programming")

    Returns:
        Summary from Wikipedia or not found message
    """
    logger.info("=" * 80)
    logger.info("📖 TOOL CALLED: wikipedia_lookup")
    logger.info(f"   Topic: '{topic}'")
    logger.info("=" * 80)

    try:
        # Run sync wikipedia-api in executor
        def _lookup():
            wiki = wikipediaapi.Wikipedia(
                user_agent="WhatsAppBot/1.0 (contact@example.com)",
                language="en",
            )
            page = wiki.page(topic)
            if not page.exists():
                return None
            # Return first ~1500 chars of summary
            summary = page.summary[:1500]
            if len(page.summary) > 1500:
                summary += "..."
            return {
                "title": page.title,
                "summary": summary,
                "url": page.fullurl,
            }

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _lookup)

        if not result:
            logger.info(f"No Wikipedia article found for: {topic}")
            return f"No Wikipedia article found for: {topic}"

        formatted = f"**{result['title']}**\n\n{result['summary']}\n\nSource: {result['url']}"

        logger.info(f"Wikipedia article found: {result['title']}")
        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: wikipedia_lookup")
        logger.info(f"   Returning {len(formatted)} characters to agent")
        logger.info("=" * 80)

        return formatted

    except Exception as e:
        logger.error(f"Wikipedia lookup failed: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: wikipedia_lookup")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Wikipedia lookup failed: {str(e)}"


@agent.tool
async def convert_units(
    ctx: RunContext[AgentDeps],
    value: float,
    from_unit: str,
    to_unit: str,
) -> str:
    """
    Convert a value from one unit to another.

    Supports many unit types:
    - Length: m, km, ft, mi, in, cm, mm, yard
    - Weight: kg, lb, oz, g, ton
    - Temperature: celsius, fahrenheit, kelvin
    - Volume: L, gal, ml, cup, pint, quart
    - Speed: m/s, km/h, mph, knot
    - Time: s, min, h, day, week
    - Area: m², ft², acre, hectare
    - And many more

    Args:
        ctx: Run context (unused but required by decorator)
        value: The numeric value to convert
        from_unit: Source unit (e.g., "km", "lb", "celsius")
        to_unit: Target unit (e.g., "miles", "kg", "fahrenheit")

    Returns:
        Converted value with units
    """
    logger.info("=" * 80)
    logger.info("🔄 TOOL CALLED: convert_units")
    logger.info(f"   Value: {value} {from_unit} -> {to_unit}")
    logger.info("=" * 80)

    try:
        # Run sync pint in executor (it's CPU-bound parsing)
        def _convert():
            quantity = value * ureg(from_unit)
            converted = quantity.to(to_unit)
            return f"{value} {from_unit} = {converted.magnitude:.4g} {to_unit}"

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _convert)

        logger.info(f"Conversion result: {result}")
        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: convert_units")
        logger.info("=" * 80)

        return result

    except pint.errors.DimensionalityError:
        error_msg = f"Cannot convert {from_unit} to {to_unit} - incompatible unit types"
        logger.error(error_msg)
        return error_msg
    except pint.errors.UndefinedUnitError as e:
        error_msg = f"Unknown unit: {e}"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        logger.error(f"Conversion failed: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: convert_units")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Conversion failed: {str(e)}"


# =============================================================================
# WhatsApp Action Tools
# =============================================================================


@agent.tool
async def send_whatsapp_reaction(ctx: RunContext[AgentDeps], emoji: str) -> str:
    """
    React to the user's message with an emoji.

    Use this when the user says something that warrants an emotional response,
    or to acknowledge receipt while working on a longer response.

    Common reactions:
    - 👍 for approval, agreement, or acknowledgment
    - ❤️ for love, appreciation, or thanks
    - 😂 for something funny
    - 😮 for surprise or amazement
    - 🙏 for gratitude or prayer

    Args:
        ctx: Run context with WhatsApp client and message info
        emoji: The emoji to react with (e.g., "👍", "❤️", "😂")

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("💬 TOOL CALLED: send_whatsapp_reaction")
    logger.info(f"   Emoji: {emoji}")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info(f"   Message ID: {ctx.deps.current_message_id}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send reaction."

    if not deps.current_message_id:
        return "No message ID available to react to."

    try:
        await deps.whatsapp_client.send_reaction(
            phone_number=deps.whatsapp_jid,
            message_id=deps.current_message_id,
            emoji=emoji,
        )

        logger.info(f"✅ Reaction {emoji} sent successfully")
        return f"Reaction {emoji} sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send reaction: {e}")
        return f"Failed to send reaction: {str(e)}"


@agent.tool
async def send_whatsapp_location(
    ctx: RunContext[AgentDeps],
    latitude: float,
    longitude: float,
    name: str | None = None,
    address: str | None = None,
) -> str:
    """
    Send a location to the user via WhatsApp.

    Use this when the user asks for directions, wants to know where something is,
    or when sharing a place would be helpful.

    Args:
        ctx: Run context with WhatsApp client
        latitude: Latitude coordinate (-90 to 90)
        longitude: Longitude coordinate (-180 to 180)
        name: Optional name for the location (e.g., "Eiffel Tower")
        address: Optional address string

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("📍 TOOL CALLED: send_whatsapp_location")
    logger.info(f"   Coordinates: {latitude}, {longitude}")
    logger.info(f"   Name: {name}")
    logger.info(f"   Address: {address}")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send location."

    # Validate coordinates
    if not -90 <= latitude <= 90:
        return f"Invalid latitude: {latitude}. Must be between -90 and 90."
    if not -180 <= longitude <= 180:
        return f"Invalid longitude: {longitude}. Must be between -180 and 180."

    try:
        await deps.whatsapp_client.send_location(
            phone_number=deps.whatsapp_jid,
            latitude=latitude,
            longitude=longitude,
            name=name,
            address=address,
        )

        location_desc = name or f"{latitude}, {longitude}"
        logger.info(f"✅ Location '{location_desc}' sent successfully")
        return f"Location '{location_desc}' sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send location: {e}")
        return f"Failed to send location: {str(e)}"


@agent.tool
async def send_whatsapp_contact(
    ctx: RunContext[AgentDeps],
    contact_name: str,
    contact_phone: str,
    contact_email: str | None = None,
    contact_organization: str | None = None,
) -> str:
    """
    Send a contact card (vCard) to the user via WhatsApp.

    Use this when sharing contact information would be helpful,
    such as business contacts, support numbers, etc.

    Args:
        ctx: Run context with WhatsApp client
        contact_name: Full name of the contact
        contact_phone: Phone number with country code (e.g., "+1234567890")
        contact_email: Optional email address
        contact_organization: Optional company/organization name

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("👤 TOOL CALLED: send_whatsapp_contact")
    logger.info(f"   Contact: {contact_name} ({contact_phone})")
    logger.info(f"   Email: {contact_email}")
    logger.info(f"   Organization: {contact_organization}")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send contact."

    try:
        await deps.whatsapp_client.send_contact(
            phone_number=deps.whatsapp_jid,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            contact_org=contact_organization,
        )

        logger.info(f"✅ Contact '{contact_name}' sent successfully")
        return f"Contact card for '{contact_name}' sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send contact: {e}")
        return f"Failed to send contact: {str(e)}"


@agent.tool
async def send_whatsapp_message(ctx: RunContext[AgentDeps], text: str) -> str:
    """
    Send an additional text message to the user via WhatsApp.

    Use this sparingly - your main response is automatically sent.
    Only use this for:
    - Follow-up information that should be in a separate message
    - Sending multiple distinct pieces of information

    Do NOT use this for your primary response to the user.

    Args:
        ctx: Run context with WhatsApp client
        text: The message text to send

    Returns:
        Success message or error description
    """
    logger.info("=" * 80)
    logger.info("📝 TOOL CALLED: send_whatsapp_message")
    logger.info(f"   Text: {text[:100]}...")
    logger.info(f"   JID: {ctx.deps.whatsapp_jid}")
    logger.info("=" * 80)

    deps = ctx.deps

    if not deps.whatsapp_client:
        return "WhatsApp client not available. Cannot send message."

    if not text.strip():
        return "Cannot send empty message."

    try:
        result = await deps.whatsapp_client.send_text(
            phone_number=deps.whatsapp_jid,
            text=text,
        )

        logger.info(f"✅ Message sent successfully (ID: {result.message_id})")
        return "Message sent successfully."

    except Exception as e:
        logger.error(f"❌ Failed to send message: {e}")
        return f"Failed to send message: {str(e)}"


async def get_ai_response(
    user_message: str,
    message_history=None,
    agent_deps: AgentDeps = None,
    image_data: str | None = None,
    image_mimetype: str | None = None,
):
    """
    Stream AI response token by token for a user message with optional history

    Args:
        user_message: The user's message
        message_history: Optional list of previous messages
        agent_deps: Optional dependencies for agent tools (enables semantic search)
        image_data: Optional base64-encoded image data for vision
        image_mimetype: Optional image MIME type (e.g., 'image/jpeg')

    Yields:
        str: Text chunks as they arrive from Gemini
    """
    has_image = image_data is not None and image_mimetype is not None

    logger.info("=" * 80)
    logger.info("🤖 AGENT STARTING")
    logger.info(f"   User message: {user_message}")
    logger.info(f"   History messages: {len(message_history) if message_history else 0}")
    logger.info(f"   Has image: {has_image}")
    logger.info(f"   Has dependencies: {agent_deps is not None}")
    if agent_deps:
        logger.info(f"   - Embedding service: {agent_deps.embedding_service is not None}")
    logger.info("=" * 80)

    # Construct the prompt - either text only or text + image
    if has_image:
        # Decode base64 image and create BinaryContent
        image_bytes = base64.b64decode(image_data)
        prompt = [
            user_message,
            BinaryContent(data=image_bytes, media_type=image_mimetype),
        ]
        logger.info(f"   Image size: {len(image_bytes)} bytes, type: {image_mimetype}")
    else:
        prompt = user_message

    # Track full response for logging
    full_response = ""

    # Use async context manager to enter streaming context
    async with agent.run_stream(prompt, message_history=message_history, deps=agent_deps) as result:
        # Call .stream_text(delta=True) to get incremental deltas (NOT cumulative text)
        async for text_chunk in result.stream_text(delta=True):
            full_response += text_chunk
            yield text_chunk

    logger.info("=" * 80)
    logger.info("✅ AGENT COMPLETED")
    logger.info(f"   Final response length: {len(full_response)} characters")
    logger.info(f"   Full response:\n{full_response}")
    logger.info("=" * 80)


def format_message_history(db_messages):
    """
    Convert database messages to Pydantic AI message format

    Args:
        db_messages: List of ConversationMessage objects

    Returns:
        List of messages in Pydantic AI format
    """
    from pydantic_ai import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    formatted = []
    for msg in db_messages:
        if msg.role == "user":
            formatted.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
        else:
            formatted.append(ModelResponse(parts=[TextPart(content=msg.content)]))

    return formatted
