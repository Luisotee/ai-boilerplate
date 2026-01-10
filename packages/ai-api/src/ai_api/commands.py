"""
Chat command parser and executor for conversation preferences.

Handles commands like /settings, /tts on, /stt lang es, etc.
Commands are intercepted before reaching the AI agent.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .database import ConversationMessage, ConversationPreferences, get_or_create_preferences
from .logger import logger

# Supported language codes
SUPPORTED_LANGUAGES = {"en", "es", "pt", "fr", "de"}

# Language display names
LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
}


@dataclass
class CommandResult:
    """Result of command execution."""

    is_command: bool
    response_text: str | None = None
    save_to_history: bool = False  # Commands should NOT be saved to history


def strip_leading_mentions(message: str) -> str:
    """Strip @mentions from the beginning of message for command parsing.

    Handles group chat messages where users mention the bot before commands,
    e.g., "@BotName /settings" -> "/settings"
    """
    return re.sub(r"^(@\S+\s*)+", "", message).strip()


def is_command(message: str) -> bool:
    """Check if message is a command (starts with / after stripping mentions)."""
    cleaned = strip_leading_mentions(message)
    return cleaned.startswith("/")


def _parse_duration(duration_str: str) -> timedelta | None:
    """Parse a duration string like '1h', '7d', '1m' into a timedelta.

    Args:
        duration_str: Duration string (e.g., '1h', '7d', '1m')

    Returns:
        timedelta object or None if invalid format
    """
    match = re.match(r"^(\d+)([hdm])$", duration_str.lower())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "m":
        return timedelta(days=value * 30)  # Approximate month as 30 days

    return None


def _format_settings(prefs: ConversationPreferences) -> str:
    """Format current settings for display."""
    tts_status = "enabled" if prefs.tts_enabled else "disabled"
    tts_lang = LANGUAGE_NAMES.get(prefs.tts_language, prefs.tts_language)
    stt_lang = (
        LANGUAGE_NAMES.get(prefs.stt_language, prefs.stt_language)
        if prefs.stt_language
        else "auto-detect"
    )

    return f"""Your current settings:
- TTS: {tts_status}
- TTS Language: {tts_lang}
- STT Language: {stt_lang}

Use /help to see available commands."""


def _get_help_text() -> str:
    """Return help text with available commands."""
    lang_codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
    return f"""Available commands:

/settings - Show current settings
/tts on - Enable voice responses
/tts off - Disable voice responses
/tts lang [code] - Set TTS language
/stt lang [code] - Set transcription language
/stt lang auto - Use auto-detection for STT
/clean - Delete all conversation history
/clean [duration] - Delete messages (e.g., 1h, 7d, 1m)
/help - Show this message

Language codes: {lang_codes}"""


def _handle_tts_command(db: Session, prefs: ConversationPreferences, parts: list[str]) -> str:
    """Handle /tts commands."""
    if len(parts) < 2:
        status = "enabled" if prefs.tts_enabled else "disabled"
        return f"TTS is currently {status}. Use '/tts on', '/tts off', or '/tts lang [code]'."

    action = parts[1].lower()

    if action == "on":
        prefs.tts_enabled = True
        db.commit()
        logger.info(f"TTS enabled for user {prefs.user_id}")
        return "TTS has been enabled. I will now respond with voice messages."

    elif action == "off":
        prefs.tts_enabled = False
        db.commit()
        logger.info(f"TTS disabled for user {prefs.user_id}")
        return "TTS has been disabled. I will respond with text only."

    elif action == "lang":
        if len(parts) < 3:
            current = LANGUAGE_NAMES.get(prefs.tts_language, prefs.tts_language)
            codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
            return f"Current TTS language: {current}. Usage: /tts lang [code]. Available: {codes}"

        lang_code = parts[2].lower()
        if lang_code not in SUPPORTED_LANGUAGES:
            codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
            return f"Invalid language code '{lang_code}'. Available: {codes}"

        prefs.tts_language = lang_code
        db.commit()
        lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)
        logger.info(f"TTS language set to {lang_code} for user {prefs.user_id}")
        return f"TTS language set to {lang_name}."

    else:
        return "Unknown TTS command. Use '/tts on', '/tts off', or '/tts lang [code]'."


def _handle_stt_command(db: Session, prefs: ConversationPreferences, parts: list[str]) -> str:
    """Handle /stt commands."""
    if len(parts) < 2:
        current = (
            LANGUAGE_NAMES.get(prefs.stt_language, prefs.stt_language)
            if prefs.stt_language
            else "auto-detect"
        )
        return f"STT language is currently: {current}. Use '/stt lang [code]' or '/stt lang auto'."

    action = parts[1].lower()

    if action == "lang":
        if len(parts) < 3:
            current = (
                LANGUAGE_NAMES.get(prefs.stt_language, prefs.stt_language)
                if prefs.stt_language
                else "auto-detect"
            )
            codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
            return (
                f"Current STT language: {current}. Usage: /stt lang [code|auto]. Available: {codes}"
            )

        lang_code = parts[2].lower()

        if lang_code == "auto":
            prefs.stt_language = None
            db.commit()
            logger.info(f"STT language set to auto-detect for user {prefs.user_id}")
            return "STT language set to auto-detect."

        if lang_code not in SUPPORTED_LANGUAGES:
            codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
            return f"Invalid language code '{lang_code}'. Available: {codes}, auto"

        prefs.stt_language = lang_code
        db.commit()
        lang_name = LANGUAGE_NAMES.get(lang_code, lang_code)
        logger.info(f"STT language set to {lang_code} for user {prefs.user_id}")
        return f"STT language set to {lang_name}."

    else:
        return "Unknown STT command. Use '/stt lang [code]' or '/stt lang auto'."


def _handle_clean_command(db: Session, user_id: str, parts: list[str]) -> str:
    """Handle /clean command to delete conversation history.

    Args:
        db: Database session
        user_id: User UUID string
        parts: Command parts (e.g., ['/clean'], ['/clean', '7d'])

    Returns:
        Response message
    """
    # Determine the time filter
    duration: timedelta | None = None
    duration_str: str | None = None

    if len(parts) >= 2:
        arg = parts[1].lower()
        if arg == "all":
            duration = None  # Delete all
        else:
            duration = _parse_duration(arg)
            if duration is None:
                return (
                    f"Invalid duration '{parts[1]}'. "
                    "Use formats like: 1h (hours), 7d (days), 1m (months), or 'all'."
                )
            duration_str = arg

    # Build the query
    query = db.query(ConversationMessage).filter(ConversationMessage.user_id == user_id)

    if duration:
        cutoff = datetime.utcnow() - duration
        query = query.filter(ConversationMessage.timestamp >= cutoff)

    # Count messages to delete
    message_count = query.count()

    if message_count == 0:
        if duration_str:
            return f"No messages found from the last {duration_str}."
        return "No messages found to delete."

    # Delete messages
    query.delete()
    db.commit()

    if duration_str:
        logger.info(f"Cleaned {message_count} messages (last {duration_str}) for user {user_id}")
        return f"Deleted {message_count} messages from the last {duration_str}."
    else:
        logger.info(f"Cleaned all {message_count} messages for user {user_id}")
        return f"Deleted all {message_count} messages. Conversation history cleared."


def parse_and_execute(db: Session, user_id: str, message: str) -> CommandResult:
    """
    Parse and execute a command message.

    Args:
        db: Database session
        user_id: User UUID string
        message: Raw message text (may include leading @mentions in groups)

    Returns:
        CommandResult with response text
    """
    # Strip leading mentions for command parsing (handles "@BotName /settings")
    cleaned_message = strip_leading_mentions(message)

    if not cleaned_message.startswith("/"):
        return CommandResult(is_command=False)

    # Parse command parts from cleaned message
    parts = cleaned_message.split()
    command = parts[0].lower()

    logger.info(f"Processing command '{command}' for user {user_id}")

    # Handle /help (no preferences needed)
    if command == "/help":
        return CommandResult(is_command=True, response_text=_get_help_text())

    # Handle /clean (no preferences needed)
    if command == "/clean":
        response = _handle_clean_command(db, user_id, parts)
        return CommandResult(is_command=True, response_text=response)

    # Get or create preferences for other commands
    prefs = get_or_create_preferences(db, user_id)

    if command == "/settings":
        return CommandResult(is_command=True, response_text=_format_settings(prefs))

    elif command == "/tts":
        response = _handle_tts_command(db, prefs, parts)
        return CommandResult(is_command=True, response_text=response)

    elif command == "/stt":
        response = _handle_stt_command(db, prefs, parts)
        return CommandResult(is_command=True, response_text=response)

    else:
        return CommandResult(
            is_command=True,
            response_text=f"Unknown command '{command}'. Use /help to see available commands.",
        )
