---
name: fleetview-check
description: >-
  Checks whether changes to /admin/*, schemas.py, routes/health.py,
  routes/knowledge_base.py, routes/broadcasts.py, routes/bug_reports.py, or
  runtime_config.py break the FleetView Next.js dashboard at
  /home/ubuntu/projects/fleetview. Use when touching admin, health, KB,
  broadcast, bug-report, or runtime_config contracts, or when the user mentions FleetView.
---

# FleetView Contract Check (`fleetview-check`)

FleetView (`/home/ubuntu/projects/fleetview`) is the Next.js 16 control-plane dashboard that manages the bot fleet. Its Zod schemas in `/home/ubuntu/projects/fleetview/lib/bot-api/schemas.ts` strictly mirror this repository's admin, health, and knowledge-base HTTP contracts.

## Guardrails (Read-Only)

- **Never modify `/home/ubuntu/projects/fleetview`** unless explicitly instructed by the user.
- **Never run write-side git, package, or DB commands in FleetView** (no `git pull/checkout/reset`, `pnpm install/build/dev`, or `db:*` scripts).
- **Never read or print `/home/ubuntu/projects/fleetview/.env*` files** (they contain live `DATABASE_URL`, `FLEETVIEW_MASTER_KEY`, and `BETTER_AUTH_SECRET`).

## Key Files to Compare

1. **FleetView (Consumer)**:
   - `/home/ubuntu/projects/fleetview/CLAUDE.md` (and `AGENTS.md`)
   - `/home/ubuntu/projects/fleetview/lib/bot-api/schemas.ts` — Zod schemas for every bot endpoint
   - `/home/ubuntu/projects/fleetview/lib/bot-api/index.ts` & `lib/actions/*` — server-side callers and write actions
2. **ai-boilerplate (Producer)**:
   - `packages/ai-api/src/ai_api/routes/admin.py`
   - `packages/ai-api/src/ai_api/routes/broadcasts.py`
   - `packages/ai-api/src/ai_api/routes/bug_reports.py`
   - `packages/ai-api/src/ai_api/routes/health.py`
   - `packages/ai-api/src/ai_api/routes/knowledge_base.py`
   - `packages/ai-api/src/ai_api/schemas.py`
   - `packages/ai-api/src/ai_api/runtime_config.py` (`REGISTRY`)

## Breakage Rules

1. **Adding new response fields is safe**: FleetView uses plain `z.object(...)`, which strips unknown keys without failing.
2. **Removing or renaming fields FleetView reads is a breaking change**: Triggers `BotContractError` on the corresponding dashboard tab.
3. **Adding values to closed `z.enum`s is a breaking change**:
   - `SettingTypeSchema = z.enum(["str", "int", "float", "bool"])`
   - `source: z.enum(["default", "override"])`
   - `conversation_type: z.enum(["private", "group"])`
   - `role: z.enum(["user", "assistant"])`
   - QR `status: z.enum(["connecting", "qr", "connected", "disconnected", "unavailable"])`
   - `KbStatusSchema` (knowledge base document statuses)
   - Broadcast enums in `schemas.py` (`BroadcastAudience`, `BroadcastPlatform`, `BroadcastStatus`, `BroadcastRecipientStatus`)
   - Note: `SettingSchema.category` is a free `z.string()`, so adding a new `runtime_config` category is safe.
4. **Changed request bodies** (`PATCH /admin/settings`, `PUT /admin/prompt`, KB upload) break the corresponding Server Action in `lib/actions/*`.

## Steps

1. Inspect `/home/ubuntu/projects/fleetview/lib/bot-api/schemas.ts` (and `lib/bot-api/index.ts`) alongside the modified boilerplate route/schema files.
2. Check every changed field, status code, request payload, and enum value against FleetView's Zod definitions.
3. Report whether the change is **Safe**, **Breaking**, or **Requires a matching FleetView update** (citing exact file paths and line numbers in both repos).
