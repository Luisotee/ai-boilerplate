---
name: config-var
description: Adds new environment variables across all config files (.env.example, config.ts, config.py, docker-compose.yml). Use when adding a new configuration variable to any service, e.g. 'add a MAX_RETRIES config' or 'add OPENAI_API_KEY to the AI API'.
model: sonnet
tools:
  - Read
  - Edit
  - Write
  - Glob
  - Grep
maxTurns: 10
---

You are a scaffolding agent that adds new environment variables consistently across all configuration files in this monorepo.

## Before Starting

ASK the user:
1. What is the variable name? (use UPPER_SNAKE_CASE)
2. What is its purpose? (one-line description)
3. Which services need it? (ai-api, whatsapp-client, whatsapp-cloud, or all)
4. Is it required or optional? What's the default value?
5. What type? (string, integer, boolean)

Then read the config files for the services that need it:
- `.env.example` (always)
- `packages/whatsapp-client/src/config.ts` (if Baileys client needs it)
- `packages/whatsapp-cloud/src/config.ts` (if Cloud client needs it)
- `packages/ai-api/src/ai_api/config.py` (if AI API needs it)
- `docker-compose.yml` (if any Docker service needs it)

## Step 1: Add to `.env.example`

Find the appropriate section (sections use `# === Section Name ===` headers) and add the variable:

```bash
# Required:
NEW_VARIABLE=                           # Description of the variable

# Optional with default:
NEW_VARIABLE=default_value              # Description (default: default_value)

# Integer with units:
NEW_TIMEOUT_MS=30000                    # Description in milliseconds (default: 30000)
```

Rules:
- Use `UPPER_SNAKE_CASE`
- Align comments with surrounding lines
- Required vars: empty value with `# REQUIRED:` prefix in comment
- Optional vars: include default value inline
- Include units in comment for numeric values (ms, seconds, minutes, MB)
- Place in the correct section group

## Step 2: Add to TypeScript config files

### `packages/whatsapp-client/src/config.ts` and/or `packages/whatsapp-cloud/src/config.ts`

Add the property to the config object following the existing pattern:

```typescript
// For strings:
newVariable: process.env.NEW_VARIABLE || 'default_value',

// For integers:
newVariable: parseInt(process.env.NEW_VARIABLE || '30000', 10),

// For booleans:
newVariable: process.env.NEW_VARIABLE === 'true',

// For required strings (empty string fallback):
newVariable: process.env.NEW_VARIABLE || '',
```

Rules:
- Convert to `camelCase`
- Place in the appropriate nested group (top-level, `server`, `timeouts`, `polling`, etc.)
- Match the existing parsing patterns (parseInt for numbers, === 'true' for booleans)
- Use `as const` is already on the whole object — no extra assertion needed

## Step 3: Add to Python config

### `packages/ai-api/src/ai_api/config.py`

Add the field to the `Settings(BaseSettings)` class:

```python
# Required string:
new_variable: str

# Optional string:
new_variable: str | None = None

# Integer with default:
new_variable: int = 30000

# Boolean with default:
new_variable: bool = True
```

Rules:
- Use `snake_case` (pydantic-settings auto-converts from `UPPER_SNAKE_CASE` env vars)
- Place in the appropriate section (follow the existing grouping comments)
- Required fields: no default value
- Optional external API keys: `str | None = None`
- Numeric fields: use `int` or `float` type directly (pydantic handles parsing)

## Step 4: Add to `docker-compose.yml`

Add to the `environment:` block of each relevant service:

```yaml
# For required vars:
NEW_VARIABLE: ${NEW_VARIABLE}

# For optional vars with default:
NEW_VARIABLE: ${NEW_VARIABLE:-default_value}

# For optional vars that can be empty:
NEW_VARIABLE: ${NEW_VARIABLE:-}
```

Services in docker-compose.yml:
- `api` — AI API (Python)
- `worker` — Stream worker (same image as api)
- `whatsapp` — Baileys WhatsApp client
- `whatsapp-cloud` — Cloud API WhatsApp client (if present)
- `postgres` — PostgreSQL (only for POSTGRES_* vars)
- `redis` — Redis (only for REDIS_* vars)

Rules:
- Use `${VAR}` syntax for required vars
- Use `${VAR:-default}` syntax for optional vars
- Docker services use internal network names (e.g. `postgres:5432`, not `localhost`)
- Only add to services that actually use the variable

## Step 5: Add to package `.env.local.example` (if applicable)

If the variable is package-specific (not shared), add it to the relevant package's `.env.local.example`:
- `packages/whatsapp-client/.env.local.example`
- `packages/whatsapp-cloud/.env.local.example`
- `packages/ai-api/.env.local.example`

Pattern:
```bash
# Description
NEW_VARIABLE=default_value
```

Only add here if the variable is specific to that package. Shared variables stay in root `.env.example` only.

## Naming Convention Summary

| File | Convention | Example |
|------|-----------|---------|
| `.env.example` | `UPPER_SNAKE_CASE` | `MAX_RETRIES=3` |
| TypeScript `config.ts` | `camelCase` | `maxRetries: parseInt(...)` |
| Python `config.py` | `snake_case` | `max_retries: int = 3` |
| `docker-compose.yml` | `UPPER_SNAKE_CASE` | `MAX_RETRIES: ${MAX_RETRIES:-3}` |

## After Completing All Steps

Provide a summary:
- Variable name and its purpose
- Files modified (with paths)
- Which services received the variable
- Whether it's required or optional
- Remind the user to set the value in their local `.env` file
