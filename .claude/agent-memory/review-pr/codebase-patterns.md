---
name: Codebase Patterns and Recurring Issues
description: Patterns validated or flagged across PRs in this monorepo
type: feedback
---

**console.warn in config.ts is acceptable (not a violation):** Both config.ts files run at module initialization, before the Pino logger is available. `console.warn` is appropriate here and is not a CLAUDE.md violation.
**Why:** Pino logger is instantiated after config is loaded — there's no way to use structured logging in config.ts module-level code.
**How to apply:** Do not flag console.warn in config.ts as a structured-logging violation.

**Redis healthcheck with requirepass:** `redis-cli ping` (no explicit auth flag) works correctly when `REDISCLI_AUTH` env var is set on the container. Docker Compose `environment:` block sets this before the healthcheck runs. Do not flag this as an auth bypass.
**Why:** REDISCLI_AUTH is the standard redis-cli mechanism for implicit auth, and the compose file sets it correctly.
**How to apply:** Only flag if REDISCLI_AUTH is absent from the container environment while requirepass is active.

**escape_sed with `|` as sed delimiter:** The function escapes `& / | \`. When sed uses `|` as delimiter (as setup.sh does), the replacement string must escape `|`. This is correctly handled. Base64-after-tr passwords are alphanumeric only so special chars from user input are the real risk — and user-typed API keys can contain chars like `+`, `.`, `-` which are NOT escaped.
**Why:** API keys from Google/Groq/Meta may contain hyphens and dots (safe in sed replacement) but also potentially `=` (stripped by the function logic only for passwords, not for API keys read via read -rsp).
**How to apply:** Flag escape_sed as potentially incomplete for user-pasted API keys containing characters outside alphanumeric + hyphen + dot.
