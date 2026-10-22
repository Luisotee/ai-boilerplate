---
name: review-pr
description: >-
  Reviews pull requests or the current branch for AGENTS.md/CLAUDE.md compliance,
  code quality, security invariants, error hygiene, and architecture patterns.
  Use when the user asks to review changes, review a branch, or review a PR number.
---

# Code & PR Review (`review-pr`)

Perform a thorough read-only code review of the current branch or a specified pull request. Diagnose issues and produce a structured report without modifying code unless asked.

## Step 1: Gather the Diff & Refresh Subsystem Rules

### Self-review mode (no PR number given)
```bash
git branch --show-current
git log main..HEAD --oneline
git diff main --stat
git diff main
```

### External PR mode (PR number given)
```bash
gh pr view <NUMBER> --json title,body,author,baseRefName,headRefName,files,additions,deletions,changedFiles
gh pr diff <NUMBER>
gh pr view <NUMBER> --json comments
```

1. Read the full changed files (not only the diff lines) to understand surrounding context.
2. Read every nested `AGENTS.md` / `CLAUDE.md` in or above the changed files' directories, plus any cross-cutting subsystem doc listed in the root `AGENTS.md` "Subsystem Docs" table (e.g., `services/AGENTS.md` for broadcasts/bug-reports/identity, `agent/AGENTS.md` for model chain/shared-group tools, `streams/AGENTS.md` for PDF queue).

## Step 2: Review Checklist

### 1. Project Invariants & Gotchas
- **TypeScript (`whatsapp-client`, `whatsapp-cloud`, `telegram-client`)**:
  - Baileys handlers wrap in `try/catch/finally` with `sendFailureReaction` in `catch` and `sendPresenceUpdate('paused')` in `finally`.
  - Cloud API handlers use `graphApi.sendReaction(senderPhone, messageId, '❌')` in `catch` and skip `paused` presence.
  - `normalizeMessageContent()` is called before type-checking Baileys messages; `contextInfo` is read from the specific message type (`imageMessage.contextInfo`).
  - Both JID (`@s.whatsapp.net`) and LID (`@lid`) formats are checked for bot mentions.
  - Shared files (`utils/whitelist.ts`, `utils/gating.ts`) remain byte-identical across clients where enforced by `whitelist-copies.test.ts`.
  - New Fastify routes are registered in both `src/main.ts` and `tests/helpers/fastify.ts` (`buildTestApp()`).
  - Multipart routes use plain JSON Schema (not Zod) and validate files with `validateMediaFile()`.
- **Python (`ai-api`)**:
  - Agent tools use `@agent.tool`, are imported in `agent/tools/__init__.py`, call `safe_rollback(ctx.deps.db)` in DB exception handlers, and return generic English error strings (never raw `str(e)`).
  - Chat streaming uses `run_stream_events` (never `agent.run_stream`).
  - Runtime settings only set `hot=True` in `runtime_config.py` if consumption sites read via `runtime_config.get()`.
  - Model output passes through `formatting.markdown_to_whatsapp`; `DEFAULT_SYSTEM_PROMPT` contains no `**`.
  - `CORSMiddleware` remains added AFTER `APIKeyMiddleware` in `main.py`.
  - Schema changes to existing tables include idempotent SQL in `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql`.
  - Changes to `/admin/*`, `schemas.py`, `/health*`, `/knowledge-base/*`, or `runtime_config.py` do not break FleetView's closed `z.enum`s or required fields (if touched, run the fleetview contract check from AGENTS.md "External Docs & Related Projects").

### 2. Security
- `X-API-Key` required on all routes except `/health*`, `/docs*`, and platform-verified `/webhook` routes.
- Group admin commands and group-mutating agent tools fail closed (`is_group_admin is True` / `group_admin_refusal`).
- Whitelist logic never consults `phone` for group JIDs and only consults normalized digits for `@s.whatsapp.net`.
- No secrets, tokens, or raw message contents leaked in logs (`include_content=False` for Logfire).

### 3. Avoid Known False Positives
- **Multibyte test strings**: Accented characters such as `'é'.repeat(...)` or `'ção'` in TypeScript tests (`api-key.test.ts`, `message-split.test.ts`, etc.) are intentional multibyte UTF-8 fixtures testing byte-length vs character-length boundaries—do **not** flag them as Portuguese leakage from `curupira-bot` or `castanha-bot` without reading the surrounding test context.

## Step 3: Output Format

Produce a concise report with:
1. **PR Summary**: Branch/PR, scope, and 1–2 sentence purpose.
2. **Critical Issues**: Bugs, security flaws, or violated `AGENTS.md` invariants (`path/to/file:line`).
3. **Warnings & Suggestions**: Edge cases, maintainability notes, or missing tests.
4. **Verdict**: `APPROVE`, `REQUEST CHANGES`, or `NEEDS DISCUSSION`.
