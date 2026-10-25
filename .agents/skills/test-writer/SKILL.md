---
name: test-writer
description: >-
  Guides writing unit, integration, and mocked tests across TypeScript (Vitest 4)
  and Python (pytest 9) packages following repo conventions, mocks, and fixtures.
  Use when adding or modifying tests.
---

# Write Tests (`test-writer`)

This repository uses Vitest 4 for TypeScript client packages and pytest 9 (with pytest-asyncio) for Python `ai-api`.

---

## 1. TypeScript Tests (Vitest 4)

### Rules & Gotchas:
- `globals: false` — explicitly import `describe`, `it`, `expect`, `vi`, `beforeEach` from `'vitest'`.
- Mock declarations (`vi.mock(...)`) MUST be placed before imports of production code.
- Always use `.js` extension in local import specifiers (NodeNext ESM).
- Singletons (`getBaileysSocket`, `isCloudApiConnected`) require `vi.resetModules()` in `beforeEach` when re-importing.
- Multibyte test strings (e.g. `'é'.repeat(...)`, `'ção'`) are intentional UTF-8 fixtures testing byte-length vs character-length limits (`timingSafeEqual`, message chunking).

### Unit Test Pattern
Location: `packages/<package>/tests/unit/<module>.test.ts`

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { myPureFunction } from '../../src/utils/my-util.js';

describe('myPureFunction', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('transforms input as expected', () => {
    expect(myPureFunction('hello')).toBe('HELLO');
  });
});
```

### Integration Test Pattern
Location: `packages/<package>/tests/integration/<route>.test.ts`

```typescript
import { describe, it, expect, vi, beforeAll, afterAll, beforeEach } from 'vitest';
import type { FastifyInstance } from 'fastify';

import { buildTestApp } from '../helpers/fastify.js';

describe('Route /feature', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns 200 on valid request', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/feature/endpoint',
      payload: { jid: '123@s.whatsapp.net' },
    });
    expect(res.statusCode).toBe(200);
  });
});
```

---

## 2. Python Tests (pytest 9)

### Rules & Gotchas:
- `asyncio_mode = auto` (in `pytest.ini`) — do NOT add `@pytest.mark.asyncio`.
- `conftest.py` sets environment variables and patches `create_engine` / `GoogleProvider` at session start. Order matters; never reorder conftest imports.
- Use `assert` statements directly.
- `tests/helpers/factories.py` provides `make_user()`, `make_conversation_message()`.

### Mocked Test Pattern
Location: `packages/ai-api/tests/mocked/test_<feature>.py`

```python
from unittest.mock import MagicMock, patch

from tests.helpers.factories import make_user


class TestFeatureLogic:
    def test_handles_event(self):
        mock_dep = MagicMock()
        mock_dep.process.return_value = "done"

        from ai_api.services.my_service import handle_event

        result = handle_event(mock_dep, "event-data")
        assert result == "done"
        mock_dep.process.assert_called_once_with("event-data")
```

### Integration Test Pattern
Location: `packages/ai-api/tests/integration/test_<routes>.py`

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

API_KEY = "test-api-key"
AUTH_HEADERS = {"X-API-Key": API_KEY}


def _make_mock_db():
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    return db


def _get_app_with_db(mock_db):
    from ai_api.database import get_db
    from ai_api.main import app

    app.dependency_overrides[get_db] = lambda: mock_db
    return app


def _cleanup():
    from ai_api.main import app
    app.dependency_overrides.clear()


class TestFeatureEndpoint:
    @patch("ai_api.main.init_db")
    @patch("ai_api.main.get_arq_redis", new_callable=AsyncMock)
    @patch("ai_api.main.cleanup_expired_documents")
    async def test_endpoint_success(self, mock_clean, mock_redis, mock_init):
        mock_db = _make_mock_db()
        app = _get_app_with_db(mock_db)
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.post("/feature", json={"key": "val"}, headers=AUTH_HEADERS)
                assert res.status_code == 200
        finally:
            _cleanup()
```

---

## 3. Running Tests

```bash
pnpm test                               # Run all tests (TS + Python)
pnpm test:ts                            # Run all TypeScript tests
pnpm test:python                        # Run all Python tests (uv run pytest)

# Single TS test
cd packages/whatsapp-client && pnpm exec vitest run tests/unit/whitelist.test.ts

# Single Python test
cd packages/ai-api && uv run pytest tests/mocked/test_tool_error_hygiene.py -k "safe_rollback"
```
