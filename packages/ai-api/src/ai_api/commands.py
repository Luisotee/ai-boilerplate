"""
Chat command parser and executor for conversation preferences.

Handles commands like /settings, /tts on, /stt lang es, etc.
Commands are intercepted before reaching the AI agent.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .database import (
    ConversationMessage,
    ConversationPreferences,
    get_or_create_core_memory,
    get_or_create_preferences,
)
from .kb_models import KnowledgeBaseDocument
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


def format_settings(prefs: ConversationPreferences) -> str:
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
/clear - Clear conversation messages
/forget - Clear messages + core memories
/reset - Reset everything (messages, memories, documents, preferences)
/memories - Show saved core memories
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


def handle_clear_command(db: Session, user_id: str) -> str:
    """Delete all conversation messages for the user.

    Args:
        db: Database session
        user_id: User UUID string

    Returns:
        Response message
    """
    message_count = (
        db.query(ConversationMessage).filter(ConversationMessage.user_id == user_id).delete()
    )
    db.commit()

    if message_count == 0:
        return "No conversation messages to clear."

    logger.info(f"Cleared {message_count} messages for user {user_id}")
    return f"Cleared {message_count} conversation messages."


def handle_forget_command(db: Session, user_id: str) -> str:
    """Delete all conversation messages and clear core memory.

    Args:
        db: Database session
        user_id: User UUID string

    Returns:
        Response message
    """
    message_count = (
        db.query(ConversationMessage).filter(ConversationMessage.user_id == user_id).delete()
    )

    mem = get_or_create_core_memory(db, user_id)
    had_memories = bool(mem.content)
    mem.content = ""

    db.commit()

    parts = []
    if message_count > 0:
        parts.append(f"{message_count} messages")
    if had_memories:
        parts.append("core memories")

    if not parts:
        return "No messages or memories to clear."

    deleted_str = " and ".join(parts)
    logger.info(f"Forgot {deleted_str} for user {user_id}")
    return f"Cleared {deleted_str}."


def handle_reset_command(db: Session, user_id: str, whatsapp_jid: str) -> str:
    """Reset everything: messages, core memory, documents, and preferences.

    Args:
        db: Database session
        user_id: User UUID string
        whatsapp_jid: WhatsApp JID for document cleanup

    Returns:
        Response message
    """
    try:
        message_count = (
            db.query(ConversationMessage).filter(ConversationMessage.user_id == user_id).delete()
        )

        mem = get_or_create_core_memory(db, user_id)
        had_memories = bool(mem.content)
        mem.content = ""

        # Query conversation-scoped documents and collect file paths before commit
        upload_dir = Path(settings.kb_upload_dir)
        docs_to_delete = (
            db.query(KnowledgeBaseDocument)
            .filter(
                KnowledgeBaseDocument.whatsapp_jid == whatsapp_jid,
                KnowledgeBaseDocument.is_conversation_scoped == True,  # noqa: E712
            )
            .all()
        )
        doc_count = len(docs_to_delete)
        file_paths = [upload_dir / doc.filename for doc in docs_to_delete]

        for doc in docs_to_delete:
            db.delete(doc)

        # Reset preferences to defaults
        prefs = get_or_create_preferences(db, user_id)
        prefs.tts_enabled = False
        prefs.tts_language = "en"
        prefs.stt_language = None

        db.commit()
    except Exception:
        db.rollback()
        raise

    # Delete files AFTER successful commit (files on disk can't be rolled back)
    for file_path in file_paths:
        if file_path.exists():
            try:
                file_path.unlink()
                logger.debug(f"Deleted file: {file_path}")
            except Exception as e:
                logger.warning(f"Failed to delete file {file_path}: {e}")

    parts = []
    if message_count > 0:
        parts.append(f"{message_count} messages")
    if had_memories:
        parts.append("core memories")
    if doc_count > 0:
        parts.append(f"{doc_count} documents")
    parts.append("preferences")

    deleted_str = " and ".join(parts)
    logger.info(f"Reset {deleted_str} for user {user_id}")
    return f"Reset complete. Cleared {deleted_str}."


def _handle_memories_command(db: Session, user_id: str, parts: list[str]) -> str:
    """Handle /memories command (show only)."""
    if len(parts) >= 2 and parts[1].lower() == "clear":
        return "Use /forget to clear messages and core memories, or /reset to clear everything."

    mem = get_or_create_core_memory(db, user_id)
    if not mem.content:
        return (
            "No core memories saved yet.\n\n"
            "The AI will automatically save important facts about you during conversations."
        )

    return f"Your core memories:\n\n{mem.content}"


# Commands that require group admin privileges
ADMIN_ONLY_COMMANDS = {"/clear", "/forget", "/reset", "/tts", "/stt", "/settings", "/memories"}


def parse_and_execute(
    db: Session,
    user_id: str,
    whatsapp_jid: str,
    message: str,
    conversation_type: str = "private",
    is_group_admin: bool | None = None,
) -> CommandResult:
    """
    Parse and execute a command message.

    Args:
        db: Database session
        user_id: User UUID string
        whatsapp_jid: WhatsApp JID for the conversation
        message: Raw message text (may include leading @mentions in groups)
        conversation_type: 'private' or 'group'
        is_group_admin: Whether the sender is a group admin (None if unknown/private)

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

    # In groups, restrict admin-only commands to group admins
    if conversation_type == "group" and command in ADMIN_ONLY_COMMANDS and is_group_admin is False:
        return CommandResult(
            is_command=True,
            response_text="Only group admins can use this command.",
        )

    # Handle /help (no preferences needed, unrestricted)
    if command == "/help":
        return CommandResult(is_command=True, response_text=_get_help_text())

    # Handle /memories
    if command == "/memories":
        response = _handle_memories_command(db, user_id, parts)
        return CommandResult(is_command=True, response_text=response)

    # Handle /clear (conversation messages only)
    if command == "/clear":
        response = handle_clear_command(db, user_id)
        return CommandResult(is_command=True, response_text=response)

    # Handle /forget (messages + core memory)
    if command == "/forget":
        response = handle_forget_command(db, user_id)
        return CommandResult(is_command=True, response_text=response)

    # Handle /reset (everything)
    if command == "/reset":
        response = handle_reset_command(db, user_id, whatsapp_jid)
        return CommandResult(is_command=True, response_text=response)

    # Get or create preferences for other commands
    prefs = get_or_create_preferences(db, user_id)

    if command == "/settings":
        return CommandResult(is_command=True, response_text=format_settings(prefs))

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
