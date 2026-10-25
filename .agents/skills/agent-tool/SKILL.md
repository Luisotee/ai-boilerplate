---
name: agent-tool
description: >-
  Scaffolds new Pydantic AI agent tools with @agent.tool decorator,
  agent/tools/__init__.py registration, system prompt updates in agent/core.py,
  AgentDeps wiring, and error hygiene. Use when adding or modifying tools for the
  AI agent.
---

# Add Agent Tool (`agent-tool`)

Follow this guide to create and register a new tool for the Pydantic AI agent in `packages/ai-api/src/ai_api/agent/`.

## Step 1: Define the Tool Function

Create or add to an appropriate module in `packages/ai-api/src/ai_api/agent/tools/` (e.g. `utility.py`, `search.py`, `settings.py`, `whatsapp.py`).

```python
from pydantic_ai import RunContext

from ...logger import logger
from ..core import AgentDeps, agent
from ._db import safe_rollback
from ._group import group_admin_refusal


@agent.tool
async def tool_name(ctx: RunContext[AgentDeps], param: str) -> str:
    """Docstring explaining what the tool does and parameter usage.

    Args:
        ctx: Run context with dependencies
        param: Description of parameter

    Returns:
        String message describing outcome or retrieved information
    """
    # 1. Enforce group admin authorization if modifying group-scoped state
    # refusal = group_admin_refusal(ctx, "update this setting")
    # if refusal:
    #     return refusal

    logger.info("=" * 80)
    logger.info("TOOL CALLED: tool_name")
    logger.info(f"   Param: '{param}'")
    logger.info("=" * 80)

    try:
        # Business logic using ctx.deps (e.g. ctx.deps.db, ctx.deps.http_client)
        result = "Result data"

        logger.info("=" * 80)
        logger.info("SUCCESS TOOL RETURNING: tool_name")
        logger.info(f"   Returning {len(result)} characters to agent")
        logger.info("=" * 80)
        return result

    except Exception as e:
        # DB rollback: safe_rollback prevents connection leaks if commit fails
        safe_rollback(ctx.deps.db)

        # Invariant (test_tool_error_hygiene.py): Log details with exc_info=True,
        # but NEVER return raw str(e) to the LLM to prevent leaking DB credentials or SQL
        logger.error(f"tool_name failed: {e}", exc_info=True)
        return "Failed to complete the operation. Please try again."
```

### Critical Invariants
1. **Decorator**: Use `@agent.tool` without parentheses.
2. **Signature**: First parameter is always `ctx: RunContext[AgentDeps]`. Return type is always `str`.
3. **Error Hygiene**: Never return raw `str(e)` to the model. Log errors via `logger.error(..., exc_info=True)` and return a generic English message (verified by `test_tool_error_hygiene.py`).
4. **Database Rollback**: Always invoke `safe_rollback(ctx.deps.db)` from `ai_api.agent.tools._db` in database exception handlers.
5. **Group Admin Protection**: Any tool mutating group settings must call `group_admin_refusal(ctx)` from `._group`.

## Step 2: Register Tool Import

Edit `packages/ai-api/src/ai_api/agent/tools/__init__.py`.
Importing the module registers the `@agent.tool` decorator with the agent instance:
```python
from . import your_module_name
```

## Step 3: Update Agent System Prompt

Edit `packages/ai-api/src/ai_api/agent/core.py`.
Add the tool description into the system prompt under the appropriate category:
- Search Tools (1-2)
- Web Tools (3-4)
- WhatsApp Action Tools (5-8)
- Utility Tools (9-12)
- Settings & Management Tools (13-16)

Format:
```markdown
N. **tool_name** - One-line description
   Use for: specific use cases
   Do NOT use for: things to avoid
```

## Step 4: Check `AgentDeps` Requirements

Inspect `AgentDeps` in `packages/ai-api/src/ai_api/agent/core.py`.
Standard available dependencies:
- `db` (Session)
- `user_id` (str)
- `whatsapp_jid` (str)
- `is_group_admin` (bool | None)
- `recent_message_ids` (list[str])
- `embedding_service` (EmbeddingService | None)
- `http_client` (httpx.AsyncClient | None)
- `whatsapp_client` (WhatsAppClient | None)

If adding a new field to `AgentDeps`, update its instantiation sites in:
- `packages/ai-api/src/ai_api/streams/processor.py`
- `packages/ai-api/src/ai_api/routes/chat.py`

## Step 5: Verification

Add mocked unit tests in `packages/ai-api/tests/mocked/`:
```bash
cd packages/ai-api && uv run pytest tests/mocked/test_tool_error_hygiene.py
cd packages/ai-api && uv run pytest tests/mocked/test_<your_tool>.py
```
