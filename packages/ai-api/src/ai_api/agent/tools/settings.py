from pydantic_ai import RunContext

from ...commands import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    _format_settings,
    _handle_clean_command,
)
from ...database import get_or_create_preferences
from ...logger import logger
from ..core import AgentDeps, agent


@agent.tool
async def get_user_settings(ctx: RunContext[AgentDeps]) -> str:
    """
    Show the user's current TTS and STT preferences.

    Use this when the user asks about their current settings, preferences,
    or configuration (e.g., "what are my settings?", "is voice enabled?").

    Args:
        ctx: Run context with database and user info

    Returns:
        Formatted string with current preferences
    """
    logger.info("=" * 80)
    logger.info("⚙️ TOOL CALLED: get_user_settings")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info("=" * 80)

    try:
        prefs = get_or_create_preferences(ctx.deps.db, ctx.deps.user_id)
        result = _format_settings(prefs)

        logger.info("✅ TOOL RETURNING: get_user_settings")
        logger.info(f"   Returning {len(result)} characters to agent")
        logger.info("=" * 80)

        return result

    except Exception as e:
        logger.error(f"Error getting user settings: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: get_user_settings")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to retrieve settings: {str(e)}"


@agent.tool
async def update_tts_settings(
    ctx: RunContext[AgentDeps],
    enabled: bool | None = None,
    language: str | None = None,
) -> str:
    """
    Update the user's text-to-speech settings.

    Use this when the user wants to enable/disable voice responses or change
    the TTS language (e.g., "turn on voice messages", "switch to Spanish",
    "disable TTS", "I want responses in Portuguese").

    Supported languages: en (English), es (Spanish), pt (Portuguese),
    fr (French), de (German).

    Args:
        ctx: Run context with database and user info
        enabled: Set to True to enable TTS, False to disable. None to leave unchanged.
        language: Language code (en, es, pt, fr, de). None to leave unchanged.

    Returns:
        Confirmation message describing what was changed
    """
    logger.info("=" * 80)
    logger.info("🔊 TOOL CALLED: update_tts_settings")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info(f"   Enabled: {enabled}")
    logger.info(f"   Language: {language}")
    logger.info("=" * 80)

    try:
        if language is not None:
            language = language.lower()
            if language not in SUPPORTED_LANGUAGES:
                codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
                return f"Invalid language code '{language}'. Available: {codes}"

        prefs = get_or_create_preferences(ctx.deps.db, ctx.deps.user_id)
        changes = []

        if enabled is not None:
            prefs.tts_enabled = enabled
            status = "enabled" if enabled else "disabled"
            changes.append(f"TTS {status}")
            logger.info(f"TTS {'enabled' if enabled else 'disabled'} for user {ctx.deps.user_id}")

        if language is not None:
            prefs.tts_language = language
            lang_name = LANGUAGE_NAMES.get(language, language)
            changes.append(f"TTS language set to {lang_name}")
            logger.info(f"TTS language set to {language} for user {ctx.deps.user_id}")

        if not changes:
            return (
                "No changes specified. Provide 'enabled' and/or 'language' to update TTS settings."
            )

        ctx.deps.db.commit()
        result = ". ".join(changes) + "."

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: update_tts_settings")
        logger.info(f"   Result: {result}")
        logger.info("=" * 80)

        return result

    except Exception as e:
        logger.error(f"Error updating TTS settings: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: update_tts_settings")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to update TTS settings: {str(e)}"


@agent.tool
async def update_stt_settings(
    ctx: RunContext[AgentDeps],
    language: str | None = None,
) -> str:
    """
    Update the user's speech-to-text language setting.

    Use this when the user wants to change the transcription language
    (e.g., "set transcription to Spanish", "use auto-detect for speech",
    "transcribe my audio in French").

    Pass language="auto" or language=None to enable auto-detection.
    Supported languages: en (English), es (Spanish), pt (Portuguese),
    fr (French), de (German), auto (auto-detect).

    Args:
        ctx: Run context with database and user info
        language: Language code (en, es, pt, fr, de) or "auto" for auto-detection.

    Returns:
        Confirmation message describing what was changed
    """
    logger.info("=" * 80)
    logger.info("🎙️ TOOL CALLED: update_stt_settings")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info(f"   Language: {language}")
    logger.info("=" * 80)

    try:
        prefs = get_or_create_preferences(ctx.deps.db, ctx.deps.user_id)

        if language is None or language.lower() == "auto":
            prefs.stt_language = None
            ctx.deps.db.commit()
            logger.info(f"STT language set to auto-detect for user {ctx.deps.user_id}")
            result = "STT language set to auto-detect."
        else:
            lang_code = language.lower()
            if lang_code not in SUPPORTED_LANGUAGES:
                codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
                return f"Invalid language code '{lang_code}'. Available: {codes}, auto"

            prefs.stt_language = lang_code
            ctx.deps.db.commit()
            lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)
            logger.info(f"STT language set to {lang_code} for user {ctx.deps.user_id}")
            result = f"STT language set to {lang_name}."

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: update_stt_settings")
        logger.info(f"   Result: {result}")
        logger.info("=" * 80)

        return result

    except Exception as e:
        logger.error(f"Error updating STT settings: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: update_stt_settings")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to update STT settings: {str(e)}"


@agent.tool
async def clean_conversation_history(
    ctx: RunContext[AgentDeps],
    duration: str | None = None,
) -> str:
    """
    Delete conversation history and associated documents.

    Use this when the user asks to clear their chat, delete messages,
    or start fresh (e.g., "clean my history", "delete last week's messages",
    "start over", "erase everything").

    WARNING: This is a destructive action. If the user's intent is ambiguous,
    ask for confirmation before calling this tool.

    Args:
        ctx: Run context with database and user info
        duration: Optional time period to delete. Examples:
            - None or "all" = delete everything
            - "1h" = last 1 hour
            - "7d" = last 7 days
            - "1m" = last 1 month (30 days)

    Returns:
        Message describing what was deleted
    """
    logger.info("=" * 80)
    logger.info("🧹 TOOL CALLED: clean_conversation_history")
    logger.info(f"   User ID: {ctx.deps.user_id}")
    logger.info(f"   WhatsApp JID: {ctx.deps.whatsapp_jid}")
    logger.info(f"   Duration: {duration}")
    logger.info("=" * 80)

    try:
        parts = ["/clean"]
        if duration and duration.lower() != "all":
            parts.append(duration)

        result = _handle_clean_command(
            ctx.deps.db,
            ctx.deps.user_id,
            ctx.deps.whatsapp_jid,
            parts,
        )

        logger.info("=" * 80)
        logger.info("✅ TOOL RETURNING: clean_conversation_history")
        logger.info(f"   Result: {result}")
        logger.info("=" * 80)

        return result

    except Exception as e:
        logger.error(f"Error cleaning conversation history: {str(e)}", exc_info=True)
        logger.info("=" * 80)
        logger.info("❌ TOOL ERROR: clean_conversation_history")
        logger.info(f"   Error: {str(e)}")
        logger.info("=" * 80)
        return f"Failed to clean conversation history: {str(e)}"
