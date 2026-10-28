# Migrations

Hand-written, idempotent SQL for column and index changes, because `create_all()` never adds columns. The rule and the apply command are in the root `CLAUDE.md` / `AGENTS.md`.

Run it in the checkout: `docker compose exec` targets this checkout's own `postgres` whatever `SERVICE_NAME` is, so on a host with several bots it can't hit another bot's database. Substitute your `POSTGRES_USER` / `POSTGRES_DB` if you changed the defaults. Current migrations: `2026-09-21-kb-file-hash.sql`, `2026-09-25-broadcast.sql` (`users.broadcast_opt_out`, `last_client_id`, `whatsapp_client_id`, `cloud_last_inbound_at`, and `broadcast_recipients.attempt_started_at`, added later, so a DB that applied an earlier version must apply it again; the `broadcasts`/`broadcast_recipients` tables come from `create_all()`).
