---
name: db-migration
description: >-
  Manages database schema changes across SQLAlchemy models (database.py,
  kb_models.py) and idempotent SQL migration files in
  packages/ai-api/docs/migrations/. Use when adding tables, columns, or indexes.
---

# Database Schema & Migration Workflow (`db-migration`)

This project uses PostgreSQL + pgvector (`Vector(3072)` via `gemini-embedding-001`) and SQLAlchemy `Base.metadata.create_all()` with **no Alembic**.

- `create_all()` only creates **missing tables** on startup.
- It **never** adds columns, alters types, or creates new indexes on existing tables.

## 1. Modifying an Existing Table (Columns / Indexes)

1. **Update the ORM model** in `packages/ai-api/src/ai_api/database.py` or `packages/ai-api/src/ai_api/kb_models.py`.
   - Use plain `String` columns for string enums (not SQLAlchemy `Enum`).
   - Define indexes in `__table_args__` so fresh `create_all()` databases match migrated databases.
2. **Write an idempotent SQL migration file** at `packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql`:
   - Every statement MUST be safe to re-run (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, etc.).
   - Column names, types, defaults, and index names must match what `create_all()` emits on a fresh DB.
3. **Document the migration** in the Database section of `AGENTS.md` and `CLAUDE.md`, and provide the rollout command:
   ```bash
   docker compose exec -T postgres psql -U aiagent -d aiagent \
     < packages/ai-api/docs/migrations/<YYYY-MM-DD>-<name>.sql
   ```

## 2. Adding a New Table

1. Define the model class extending `Base` in `database.py` or `kb_models.py` (ensure it is imported before `init_db()` runs `Base.metadata.create_all()`).
2. If the new table also adds foreign keys or columns to existing tables (e.g., `users`), still include an idempotent SQL file in `packages/ai-api/docs/migrations/` for those `ALTER TABLE` statements.
3. Update the table count and list in `AGENTS.md` and `CLAUDE.md`.
