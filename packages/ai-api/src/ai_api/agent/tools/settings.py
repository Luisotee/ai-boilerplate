"""Chat settings and data deletion, by asking the bot in plain words.

``settings`` is one tool with an ``action`` (it replaced ``get_user_settings``,
``update_tts_settings``, ``update_stt_settings`` and
``set_broadcast_subscription``): one schema instead of four in every model
request. ``clean_user_data`` stays separate because it is destructive and the
prompt makes the model confirm it first.
"""

from typing import Literal

from pydantic_ai import RunContext

from ...commands import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    handle_clean_command,
    set_broadcast_opt_out,
    settings_view,
)
from ...database import get_or_create_preferences, is_group_jid
from ...logger import logger
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal

SettingsAction = Literal["show", "tts", "stt", "announcements"]


@agent.tool
async def settings(
    ctx: RunContext[AgentDeps],
    action: SettingsAction,
    enabled: bool | None = None,
    language: str | None = None,
) -> str:
    """
    Show or change this chat's settings.

    - "show": the current settings ("what are my settings?").
    - "tts": your replies as audio/voice messages; enabled turns them on/off,
      language sets their language ("stop sending me audio" = enabled=false).
    - "stt": the language voice messages are transcribed in; language="auto" auto-detects.
      Transcription itself can't be turned off.
    - "announcements": the operators' update messages; enabled=false stops them
      ("stop sending me news"), enabled=true resumes them.

    Languages: en, es, pt, fr, de. In a group these are the group's settings and
    only a group admin may change them.

    Args:
        action: Which setting to show or change
        enabled: For "tts" and "announcements": on (true) or off (false)
        language: For "tts" and "stt": a language code, or "auto" for "stt"
    """
    logger.info(
        f"⚙️ TOOL CALLED: settings action={action} enabled={enabled} language={language} "
        f"user={ctx.deps.user_id}"
    )
    if action == "show":
        return await _show(ctx.deps)
    if action == "tts":
        return await _update_tts(ctx.deps, enabled, language)
    if action == "stt":
        return await _update_stt(ctx.deps, language)
    if action == "announcements":
        if enabled is None:
            return "Pass enabled=true to receive announcements or enabled=false to stop them."
        return await _set_announcements(ctx.deps, enabled)
    return 'Unknown action. Use "show", "tts", "stt" or "announcements".'


async def _show(deps: AgentDeps) -> str:
    try:
        prefs = get_or_create_preferences(deps.db, deps.user_id)
        return settings_view(deps.db, deps.user_id, prefs)  # same text as /settings
    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error getting user settings: {str(e)}", exc_info=True)
        return "Failed to retrieve settings. Please try again."


async def _update_tts(deps: AgentDeps, enabled: bool | None, language: str | None) -> str:
    refusal = group_admin_refusal(deps, "change this group's voice settings", "/tts")
    if refusal:
        return refusal

    try:
        if language is not None:
            language = language.lower()
            if language not in SUPPORTED_LANGUAGES:
                codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
                return f"Invalid language code '{language}'. Available: {codes}"

        prefs = get_or_create_preferences(deps.db, deps.user_id)
        changes = []

        if enabled is not None:
            prefs.tts_enabled = enabled
            changes.append(f"TTS {'enabled' if enabled else 'disabled'}")

        if language is not None:
            prefs.tts_language = language
            changes.append(f"TTS language set to {LANGUAGE_NAMES.get(language, language)}")

        if not changes:
            return (
                "No changes specified. Provide 'enabled' and/or 'language' to update TTS settings."
            )

        deps.db.commit()
        result = ". ".join(changes) + "."
        logger.info(f"TTS settings for user {deps.user_id}: {result}")
        return result

    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error updating TTS settings: {str(e)}", exc_info=True)
        return "Failed to update TTS settings. Please try again."


async def _update_stt(deps: AgentDeps, language: str | None) -> str:
    # No language is not a reset: "stop transcribing" arrives as enabled=false
    # with no language, and must not silently drop a configured language.
    if language is None:
        codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
        return (
            "Nothing changed: voice messages are always transcribed; transcription "
            f'can\'t be turned off. Pass language ({codes}) or "auto" to auto-detect.'
        )
    refusal = group_admin_refusal(deps, "change this group's transcription settings", "/stt")
    if refusal:
        return refusal

    try:
        prefs = get_or_create_preferences(deps.db, deps.user_id)

        if language.lower() == "auto":
            prefs.stt_language = None
            deps.db.commit()
            result = "STT language set to auto-detect."
        else:
            lang_code = language.lower()
            if lang_code not in SUPPORTED_LANGUAGES:
                codes = ", ".join(sorted(SUPPORTED_LANGUAGES))
                return f"Invalid language code '{lang_code}'. Available: {codes}, auto"

            prefs.stt_language = lang_code
            deps.db.commit()
            result = f"STT language set to {LANGUAGE_NAMES.get(lang_code, lang_code)}."

        logger.info(f"STT settings for user {deps.user_id}: {result}")
        return result

    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error updating STT settings: {str(e)}", exc_info=True)
        return "Failed to update STT settings. Please try again."


async def _set_announcements(deps: AgentDeps, subscribed: bool) -> str:
    try:
        refusal = group_admin_refusal(
            deps, "turn announcements on or off for this group", "/broadcast off"
        )
        if refusal:
            return refusal
        is_group = is_group_jid(deps.whatsapp_jid)

        # Identity comes from the run's own user row (deps.user_id), never from
        # arguments; None means that row doesn't exist.
        previous = set_broadcast_opt_out(deps.db, deps.user_id, opt_out=not subscribed)
        if previous is None:
            return "Failed to update the announcement setting. Please try again."
        target = "this group" if is_group else "you"
        if subscribed:
            if previous is False:
                return f"Announcements were already on for {target}; nothing changed."
            return f"Announcements are on again for {target}. (/broadcast off stops them.)"
        if previous is True:
            return f"Announcements were already off for {target}; nothing changed."
        return f"Announcements are now off for {target}. (/broadcast on turns them back on.)"
    except Exception as e:
        safe_rollback(deps.db)
        logger.error(f"Error updating broadcast subscription: {e}", exc_info=True)
        return "Failed to update the announcement setting. Please try again."


@agent.tool
async def clean_user_data(
    ctx: RunContext[AgentDeps],
    level: Literal["messages", "data", "all"] = "messages",
) -> str:
    """
    Delete this chat's data ("clear my history", "forget everything about me", "start over").

    Destructive and can't be undone. For level "data" or "all", ALWAYS tell the user
    what will be deleted and wait for their yes before calling; for "messages",
    confirm when the intent is at all unclear. In a group it clears the whole
    group's data and only a group admin may do it.

    Args:
        level: "messages" = the conversation only; "data" = messages and the chat's
            documents; "all" = everything, including your notes and the settings
    """
    logger.info(f"🧹 TOOL CALLED: clean_user_data level={level} user={ctx.deps.user_id}")

    # Same rule as the /clean command: wiping a group's transcript is admin-only.
    refusal = group_admin_refusal(ctx.deps, "delete this group's data", "/clean")
    if refusal:
        return refusal

    try:
        result = handle_clean_command(
            ctx.deps.db, ctx.deps.user_id, ctx.deps.whatsapp_jid, level=level
        )
        logger.info(f"clean_user_data result: {result}")
        return result

    except Exception as e:
        safe_rollback(ctx.deps.db)
        logger.error(f"Error cleaning user data: {str(e)}", exc_info=True)
        return "Failed to clean user data. Please try again."
