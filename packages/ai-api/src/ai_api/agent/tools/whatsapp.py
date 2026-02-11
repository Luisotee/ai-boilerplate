from pydantic_ai import RunContext

from ...logger import logger
from ..core import AgentDeps, agent


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
