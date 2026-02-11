import asyncio

import pint
import wikipediaapi
from pydantic_ai import RunContext
from simpleeval import simple_eval

from ...logger import logger
from ..core import AgentDeps, agent

# Unit registry for conversions (created once at module load)
ureg = pint.UnitRegistry()


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
