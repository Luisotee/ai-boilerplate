# Scheduled tasks and reminders

Users ask the bot, in plain words, for a one-time or recurring message in the current chat ("remind me tomorrow at 9 to call Ana", "every Monday at 8 send me the week's river forecast"). Operators see and manage every task through `/admin/scheduled-tasks`. The short rule list is in `src/ai_api/services/CLAUDE.md`; this file has the detail.

## Pieces

| Where | What |
|---|---|
| `database.py` `ScheduledTask`, `TaskRun` | `scheduled_tasks` (one row per task, `next_run_at` armed) and `task_runs` (one row per fired slot, `UNIQUE(task_id, scheduled_for)`). New tables: `create_all()` creates them, no migration SQL |
| `services/schedule.py` | Pure: the schedule grammar, cron compile (via `croniter`), `next_fire`, catch-up window, frequency checks |
| `services/scheduled_tasks.py` | `create_task` (limits), `list_tasks`, `find_task` / `cancel_task` (scoped to the chat), `cancel_chat_tasks` (`/clean all`), `bot_timezone()`, `rearm` |
| `agent/tools/scheduled_tasks.py` | The `manage_scheduled_tasks` tool (`action` = create / list / cancel) |
| `streams/scheduler.py` | Fourth stream-worker task: claims due rows, advances them, enqueues runs, sweeps orphans |
| `streams/scheduled_runner.py` | Runs one fired slot: claim, send-time checks, text, delivery, outcome |
| `streams/consumer.py` | A `trigger=scheduled` entry goes to the runner instead of the chat processor |
| `routes/scheduled_tasks.py` | `/admin/scheduled-tasks` |

## Kinds

- **`reminder`**: sends `⏰ <text>` (≤1000 chars). No LLM call.
- **`agent`**: runs `<text>` (≤2000 chars) as an instruction in a **fresh session** (no chat history; core memory and the system prompt still apply) and sends the result. The prompt (`scheduled_runner.agent_prompt`, the user turn, with the fire time on the last line so the stable text caches) says to answer exactly `[SILENT]` when there is nothing new; any reply containing the token, or an empty one, sends nothing and records `silent`. `AgentDeps.scheduled_run=True` makes the agent-wide `prepare_tools` (`agent/core.py` `scheduled_run_tools`) keep only the read-only `SCHEDULED_RUN_TOOLS`: nothing that sends (it would bypass the run's checks), changes settings or memory, or schedules more tasks (recursion guard, also in the prompt). `is_group_admin` is None, so group-changing tools would refuse anyway. A model error records `failed/model_error` and sends nothing (no unsolicited "Sorry"). `---` burst lines become blank lines (send-text doesn't split them). An agent schedule may fire at most 4 times a day (`AGENT_MAX_DAILY_FIRES`).

## Schedule grammar (`services/schedule.py`)

The model passes the user's words in this grammar and never computes a date: once — `in 30m`, `in 2h`, `in 1d`, `in 1h30m`, `at 18:30` (next one), `today|tomorrow at 9:00`, `monday at 9:00` (next Monday), `2026-11-20 09:00` / ISO (naive = local); repeating — `every 2h`, `every day at 9:00`, `daily at 9`, `every weekday at 8:30`, `every weekend at 10`, `every monday,friday at 9:00`, or a 5-field cron. Times accept `9`, `9:30`, `9am`, `9pm`, `21h`, `9h30`. Named forms compile to cron, so weekdays use cron numbering only (Sunday = 0). A one-shot must be in the future and within a year.

**Time zone**: `BOT_TIMEZONE` (hot), empty = `BROADCAST_TIMEZONE`, never the server's. It is snapshotted on the task at creation (`scheduled_tasks.timezone`), so a change applies to new tasks; cron slots follow DST in that zone.

## Firing (`streams/scheduler.py`, every 15 s)

1. `SELECT … FROM scheduled_tasks WHERE status='active' AND next_run_at <= now … FOR UPDATE SKIP LOCKED` (50 per tick), so several workers never take one row.
2. Per row, in the **same commit**: `next_run_at` moves to the first slot after now (NULL for a one-shot) and the slot's `task_runs` row is inserted (in a savepoint; a slot that already has a run is never fired again). **At most once**: a crash after the commit loses a slot, never doubles it.
3. **Downtime**: a recurring slot fires only if it is less late than its catch-up window (half its period, clamped to 2 min - 2 h); otherwise it is recorded `skipped/missed`. Since the next slot is the first after now, N missed slots never become N sends. A one-shot fires up to 2 h late (`ONE_SHOT_GRACE`); later, it is `skipped/missed` and the task `disabled` (`disabled_reason=missed`), so it shows in `/admin` instead of vanishing.
4. `XADD stream:user:{chat id} {trigger: scheduled, run_id, user_id}`, then mark the run `dispatched`. Going through the chat's stream keeps it in order with the chat's own messages. **Pipeline note**: such an entry must never be merged into a message burst or deduplicated against a user message.
5. Orphan sweep: `queued` > 5 min → `failed/enqueue_failed`; `dispatched` > 1 h → `failed/expired` (not sent hours late); `running` > 15 min → `unknown` (crashed mid-send; never re-sent).
6. `SCHEDULED_TASKS_ENABLED=false` stops firing (the sweep still runs); due tasks catch up by the rules above when it is turned back on.

## Running a slot (`streams/scheduled_runner.py`)

1. **Claim**: `UPDATE task_runs SET status='running' WHERE id=… AND status IN ('queued','dispatched')`, committed. A lost claim does nothing.
2. **Send-time checks** (like broadcasts), each a `skipped` run: task no longer active (`cancelled`), chat row gone (`user_deleted`), whitelist (`not_whitelisted`; the chat's own rule, so under `GROUP_GATING=membership` a group passes), Cloud chat outside the 23 h window (`cloud_window`; templates are not implemented, the tool warns at creation), more than `SCHEDULED_TASKS_MAX_RUNS_PER_DAY` sent to the chat in 24 h (`daily_limit`).
3. **Delivery**: the broadcast sender's `_deliver` (typing first on Baileys) on the task's own client (`client_id`); Telegram gets the 120 s timeout. Baileys sends from one worker process are spaced 3-8 s apart, so many tasks due at 09:00 don't burst. No daytime window: the user chose the time.
4. **Outcome** via `classify_outcome`: sent → run `sent` and the text saved to the chat's history as an `assistant` message, in one commit (retried 3×; if that fails the run stays `running` and is swept to `unknown`). 400/403/404/422 → `failed` with the code; 5xx, timeouts, `success:false` → `unknown`, never retried; 503 and connect errors (provably nothing sent) → `failed`, and a one-shot is re-armed 2 min later (3 attempts in all).
5. **Auto-disable**: every outcome other than `sent`, `silent` and a `cancelled` skip counts toward `consecutive_failures`; at 3 the task is `disabled` (`disabled_reason=consecutive_failures`). A one-shot ends `completed` when delivered (or silent) and `disabled` otherwise.

The runner never raises into the chat consumer.

## Rules

- A task always delivers to the chat it was created in (`user_id` = that chat's row, `address` = its JID, both from `AgentDeps`): a group's task only to that group, a private task never to a group.
- In a group, `create` and `cancel` need `is_group_admin is True` (`_group.group_admin_refusal`, fail closed); any member may `list`. `cancel` takes the 8-char id prefix and only matches this chat's tasks.
- Limits (hot): `SCHEDULED_TASKS_MAX_PER_CHAT` active tasks (10), `SCHEDULED_TASKS_MIN_INTERVAL_MINUTES` between two runs of a recurring task (60, checked over the next 8 days of slots), `SCHEDULED_TASKS_MAX_RUNS_PER_DAY` per chat (24).
- `/clean all` cancels the chat's active and disabled tasks; deleting a chat row (a `/link` orphan) deletes its tasks and runs (`ON DELETE CASCADE`).

## `/admin/scheduled-tasks`

`GET` (filters `status`, `kind`, `limit` ≤200, `offset`; ordered by `updated_at desc`; `counts_by_status`), `GET /{id}` (adds `cron_expr`, `interval_seconds`, `run_at` and the last 20 runs; rate-limit exempt), `PATCH /{id}` `{status: active|cancelled}` (re-enable resets the failure count and arms the next slot after now; a past one-shot, or a completed/cancelled task, is a 409), `DELETE /{id}`. `GET /admin/overview` has `disabled_scheduled_tasks`. `ScheduledTaskStatus`, `ScheduledTaskKind` and `TaskRunStatus` become closed FleetView enums once it mirrors them; `last_error`, `error_code` and `disabled_reason` are open strings.

## Verified against real Postgres

4 concurrent `claim_due` over 40 due tasks → 40 runs, one per task; a second tick fires nothing; a task re-armed onto a slot that already ran fires nothing and moves on; 8 concurrent run claims → 1 winner; deleting the user row removes its tasks and runs; the partial index `ix_scheduled_tasks_due` and `uq_task_runs_slot` are created by `create_all()`.
