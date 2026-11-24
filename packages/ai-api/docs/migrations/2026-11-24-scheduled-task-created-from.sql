-- Scheduling into a group from a private chat: `scheduled_tasks.created_from_user_id`
-- is the private requester's users row (NULL for every task created in its own
-- chat). Before every run the scheduler re-checks live that this person is still
-- a member and an admin of the group.
--
-- There is no Alembic in this project: `create_all()` never adds columns to an
-- existing table. Apply by hand (substitute your POSTGRES_USER / POSTGRES_DB if
-- you changed them):
--
--   docker compose exec -T postgres psql -U aiagent -d aiagent \
--     < packages/ai-api/docs/migrations/2026-11-24-scheduled-task-created-from.sql
--
-- Safe to re-run (IF NOT EXISTS). Apply it BEFORE deploying the new API image:
-- the model selects `created_from_user_id`, so every scheduled-task query fails
-- until the column exists. The foreign key is created with the column (named
-- `scheduled_tasks_created_from_user_id_fkey`, as `create_all()` names it), and
-- the index is named the way `Column(..., index=True)` emits it.

ALTER TABLE scheduled_tasks
    ADD COLUMN IF NOT EXISTS created_from_user_id UUID
    REFERENCES users (id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_scheduled_tasks_created_from_user_id
    ON scheduled_tasks (created_from_user_id);
