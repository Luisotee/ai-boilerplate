# Testing patterns

Detail behind the Testing section of the root `CLAUDE.md` / `AGENTS.md`. Read it before writing or restructuring tests.

Each package has `tests/` with `unit/` (pure functions, no I/O), `integration/` (HTTP routes via app injection) and `helpers/` (factories + test app builders); ai-api also has `mocked/` (DB, Redis, Google API mocked). TypeScript uses Vitest 4 (`vitest.config.ts`, tests match `tests/**/*.test.ts`); Python uses pytest 9 + pytest-asyncio (`pytest.ini`, `asyncio_mode = auto`, so no `@pytest.mark.asyncio` is needed). There is no coverage tooling (no `pytest-cov` or `@vitest/coverage-*`) and no CI/CD pipeline runs tests: testing is local only.

- **TS integration tests**: `buildTestApp()` from `tests/helpers/fastify.ts` builds Fastify with all routes but NO auth, rate limiting or Swagger. New routes must also be registered there, or integration tests won't see them
- **TS fixtures**: `makeMockSocket()` / `makeMockGraphApi()` and message factories (`makeTextMsg`, `makeWebhookBody`, etc.) in `tests/helpers/fixtures.ts`
- TS singleton state (`getBaileysSocket`, `isCloudApiConnected`) needs `vi.resetModules()` in `beforeEach` to reset between tests. `fetch` tests use `vi.stubGlobal('fetch', mockFetch)` + `vi.useFakeTimers()` for timeouts
- **Python `conftest.py`** sets env vars and applies session-scoped patches for `sqlalchemy.create_engine` and `GoogleProvider` BEFORE any production import, which prevents real DB/API connections. Order matters: don't rearrange it
- **Python integration tests**: `httpx.AsyncClient` with `ASGITransport(app=app)` + `app.dependency_overrides[get_db]` for a mock DB. The rate limiter is disabled via `tests/integration/conftest.py`
- **Python factories**: `tests/helpers/factories.py` (`make_conversation_message()`, `make_user()`, `make_http_response()`) return `MagicMock` objects mimicking ORM models
