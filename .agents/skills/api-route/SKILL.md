---
name: api-route
description: >-
  Scaffolds new REST API endpoints for the Python AI API (FastAPI) or TypeScript
  messaging clients (Fastify) with auth, rate limiting, schemas, and router
  registration. Use when adding or modifying an API endpoint.
---

# Add API Route (`api-route`)

This monorepo exposes REST endpoints via two frameworks:
- **FastAPI** (Python port 8000) in `packages/ai-api/`
- **Fastify** (TypeScript port 3001, 3002, 3003) in `packages/whatsapp-client/`, `whatsapp-cloud/`, and `telegram-client/`

---

## 1. Python AI API Routes (FastAPI)

### Step 1: Create or Extend Router
Location: `packages/ai-api/src/ai_api/routes/`

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..config import settings
from ..database import get_db
from ..deps import limiter
from ..logger import logger
from ..schemas import MyRequestSchema, MyResponseSchema

router = APIRouter(prefix="/my-feature", tags=["MyFeature"])


@router.post("", response_model=MyResponseSchema)
@limiter.limit(f"{settings.rate_limit_expensive}/minute")
async def create_feature(
    request: Request,  # Required by slowapi limiter
    body: MyRequestSchema,
    db: Session = Depends(get_db),
):
    try:
        # Route logic
        return MyResponseSchema(status="ok")
    except Exception as e:
        logger.error(f"Feature creation failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create feature",
        )
```

### Invariants:
- **Auth**: Protected by `APIKeyMiddleware` via `X-API-Key` (except `/health*` and `/docs*`).
- **Middleware Order**: In `packages/ai-api/src/ai_api/main.py`, `CORSMiddleware` MUST be added **after** `APIKeyMiddleware` (Starlette LIFO execution).
- **FleetView Contract**: If modifying `/admin/*`, `/health*`, `/knowledge-base/*`, or `schemas.py`, ensure changes do not break FleetView's Zod schemas (`lib/bot-api/schemas.ts`).

### Step 2: Register Schemas and Router
1. Define schemas in `packages/ai-api/src/ai_api/schemas.py`.
2. Include the router in `packages/ai-api/src/ai_api/main.py`:
   ```python
   from .routes.my_feature import router as my_feature_router
   app.include_router(my_feature_router)
   ```

---

## 2. TypeScript Client Routes (Fastify)

### Step 1: Standard JSON Route
Location: `packages/<client-pkg>/src/routes/`

```typescript
import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { z } from 'zod';
import { logger } from '../logger.js';

export async function registerMyFeatureRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().post(
    '/feature/endpoint',
    {
      schema: {
        body: z.object({
          jid: z.string(),
        }),
        response: {
          200: z.object({ success: z.boolean() }),
        },
      },
    },
    async (request, reply) => {
      const { jid } = request.body;
      return { success: true };
    }
  );
}
```

### Step 2: Multipart Media Route (Uploads)
Multipart routes CANNOT use Zod type provider. Use JSON Schema with `validatorCompiler`:

```typescript
app.post(
  '/feature/upload',
  {
    schema: {
      consumes: ['multipart/form-data'],
      body: {
        type: 'object',
        properties: {
          jid: { type: 'string' },
          file: { type: 'string', format: 'binary' },
        },
        required: ['jid', 'file'],
      },
    },
    validatorCompiler: () => (data: unknown) => ({ value: data }),
  },
  async (request, reply) => {
    const body = request.body as Record<string, { value: string } | unknown>;
    const jid = (body.jid as { value: string }).value;
    // Validate uploaded file using validateMediaFile()
  }
);
```

### Step 3: Register in App and Test Helper
1. Register in `packages/<client-pkg>/src/main.ts`:
   ```typescript
   await registerMyFeatureRoutes(app);
   ```
2. **Critical Invariant**: Register also in `packages/<client-pkg>/tests/helpers/fastify.ts` (`buildTestApp()`) so integration tests can reach the route without auth/rate-limiting wrappers.
