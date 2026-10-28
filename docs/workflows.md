# Common workflows

Step lists behind the Common Workflows section of the root `CLAUDE.md` / `AGENTS.md`. The `/agent-tool` and `/api-route` skills follow them.

## Adding a new agent tool (ai-api) (use `/agent-tool` skill)
1. Add the tool function in `agent/tools/` with the `@agent.tool` decorator (see existing tools for patterns)
2. Signature: `async def tool_name(ctx: RunContext[AgentDeps], ...params) -> str`; deps come from `ctx.deps` (db, embedding_service, whatsapp_client, etc.)
3. Import the module in `agent/tools/__init__.py`, or the decorator never registers the tool
4. Add the tool description to the system prompt in `agent/core.py`
5. Add mocked tests in `tests/mocked/` (mock external deps, test tool behavior)
6. Exception handlers that touch the DB must call `safe_rollback(ctx.deps.db)` (`agent/tools/_db.py`): all tools share one DB session per agent run, so a failed flush/commit leaves it dirty and later tool calls hit `PendingRollbackError`. `safe_rollback` swallows (and logs) a failing rollback, so it can never replace the tool's reply with a crash
7. Error return values must be generic English, never raw `str(e)`, because SQLAlchemy/httpx exceptions can leak DB hostnames, table names, SQL or internal URLs to the LLM and then the user. Details go to `logger.error(..., exc_info=True)`. Pinned by `tests/mocked/test_tool_error_hygiene.py`

## Adding a new API endpoint (ai-api) (use `/api-route` skill)
1. Add the route in the appropriate `routes/*.py` file (or a new router module), using `APIRouter` with tags; import `limiter` from `deps.py` for rate-limited endpoints
2. Add Pydantic schemas in `schemas.py`
3. Register a new router in `main.py` via `app.include_router()`
4. Add integration tests in `tests/integration/` using `httpx.AsyncClient` with `ASGITransport`

The Baileys handler, Cloud handler and Baileys multipart-route workflows are in the matching package's `CLAUDE.md`.
