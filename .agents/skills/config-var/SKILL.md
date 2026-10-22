---
name: config-var
description: >-
  Adds a new environment variable consistently across .env.example, TypeScript
  config.ts files, Python config.py, runtime_config.py, and docker-compose.yml.
  Use when adding or modifying configuration variables or settings.
---

# Add Configuration Variable (`config-var`)

When adding a new environment variable to any service in this monorepo, update all relevant configuration layers in order:

## Step 1: Root `.env.example` (Required for `./setup.sh --update`)

- Place the variable in the matching `# === Section ===` of `.env.example`.
- **Crucial for `./setup.sh --update` (`instance_check.py update`)**: Put a `#` comment line **directly above** the variable (it is copied into existing `.env` files and shown as the interactive prompt hint).
- Leave the value empty (`KEY=`) when it is a secret (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_PASS`) or opt-in; otherwise provide the default value.
- **Never duplicate shared secrets in `packages/*/.env.local.example`** — `.env.local` is strictly for per-developer overrides (port, log level, local flags).

## Step 2: TypeScript Clients (`packages/*/src/config.ts`)

If used by `whatsapp-client`, `whatsapp-cloud`, and/or `telegram-client`:
- Add the `camelCase` property to the `config` object in each relevant client's `src/config.ts`:
  - String: `newVar: process.env.NEW_VAR || 'default'`
  - Integer: `newVar: parseInt(process.env.NEW_VAR || '30000', 10)`
  - Boolean: `newVar: process.env.NEW_VAR === 'true'`

## Step 3: Python AI API (`packages/ai-api/src/ai_api/config.py` & `runtime_config.py`)

1. Add the `snake_case` field to `Settings(BaseSettings)` in `packages/ai-api/src/ai_api/config.py`.
2. If the setting should be visible or overridable in `/admin/settings` (and FleetView):
   - Register it in `REGISTRY` in `packages/ai-api/src/ai_api/runtime_config.py`.
   - **Invariant**: Only set `hot=True` if every consumption site reads it via `runtime_config.get("key")`. Bootstrap-only settings (DB, Redis, CORS, rate limits) must use `hot=False`.
   - Keep `type` within FleetView's closed enum (`"str"`, `"int"`, `"float"`, `"bool"`).

## Step 4: `docker-compose.yml`

Pass the variable in the `environment:` section of each container that reads it (`api`, `worker`, `whatsapp`, `whatsapp-cloud`, `telegram`):
- Required: `NEW_VAR: ${NEW_VAR}`
- Optional with default: `NEW_VAR: ${NEW_VAR:-default}`
- Optional empty: `NEW_VAR: ${NEW_VAR:-}`
