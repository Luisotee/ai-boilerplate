---
name: slash-command
description: >-
  Scaffolds new WhatsApp bot slash commands with handler functions, dispatch
  registration, help text, and admin restrictions. Use when adding or modifying
  bot commands like /settings, /tts, /clean, or /broadcast.
---

# Add Slash Command (`slash-command`)

Bot slash commands provide synchronous user controls (e.g. `/settings`, `/tts`, `/clean`) intercepted in `packages/ai-api/src/ai_api/routes/chat.py` before hitting the Redis Stream or LLM agent.

## Step 1: Add Command Handler Function

Location: `packages/ai-api/src/ai_api/commands.py`

```python
def _handle_feature_command(
    db: Session,
    prefs: ConversationPreferences,
    parts: list[str],
) -> str:
    """Handle /feature [on|off] commands."""
    if len(parts) < 2:
        return "Usage: /feature [on|off]"

    action = parts[1].lower()
    if action == "on":
        prefs.feature_enabled = True
        db.commit()
        logger.info(f"Feature enabled for user {prefs.user_id}")
        return "Feature enabled."
    elif action == "off":
        prefs.feature_enabled = False
        db.commit()
        logger.info(f"Feature disabled for user {prefs.user_id}")
        return "Feature disabled."
    else:
        return "Unknown action. Use '/feature on' or '/feature off'."
```

## Step 2: Register Dispatch in `parse_and_execute()`

In `packages/ai-api/src/ai_api/commands.py`, add to `parse_and_execute()`:

```python
elif command == "/feature":
    response = _handle_feature_command(db, prefs, parts)
    return CommandResult(is_command=True, response_text=response)
```

## Step 3: Update Help Text

Add the command to `_get_help_text()` in `commands.py`:

```python
/feature on - Enable feature
/feature off - Disable feature
```

## Step 4: Enforce Group Admin Authorization

If the command affects group-wide preferences or deletes data, add it to `ADMIN_ONLY_COMMANDS` in `commands.py`:

```python
ADMIN_ONLY_COMMANDS = {
    "/clean",
    "/tts",
    "/stt",
    "/settings",
    "/memories",
    "/broadcast",
    "/feature",
}
```

In groups, `parse_and_execute()` verifies `is_group_admin is True` before executing any command in this set. If non-admin, it fails closed and returns an admin-required error message.

## Step 5: Update Route Docstring

Update the docstring for `/chat/enqueue` in `packages/ai-api/src/ai_api/routes/chat.py` to list the new command.
