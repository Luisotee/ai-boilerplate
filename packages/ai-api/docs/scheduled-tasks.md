# Scheduled tasks and reminders

Users can ask the bot, in plain words, for a one-time or recurring message in the current chat, for example "remind me tomorrow at 9 to call Ana" or "every Monday at 8 send me the week's river forecast". Operators see and manage every task through `/admin/scheduled-tasks`. The short list of rules is in `src/ai_api/services/CLAUDE.md`; this file has the detail.

## Pieces

| Where | What |
|---|---|
| `database.py` `ScheduledTask`, `TaskRun` | `scheduled_tasks` holds one row per task, with `next_run_at` armed. `task_runs` holds one row per fired slot, with `UNIQUE(task_id, scheduled_for)` and the text to send in `message`. Both are new tables, so `create_all()` creates them and there is no migration SQL (see below). |
| `services/schedule.py` | Pure code: the schedule grammar, compiling it to cron (via `croniter`), `next_fire`, the catch-up window and the frequency checks. |
| `services/scheduled_tasks.py` | `create_task` (applies the limits), `list_tasks`, `find_task` / `cancel_task` (scoped to the chat), `cancel_chat_tasks` (used by `/clean all`), `bot_timezone()` and `rearm`. |
| `agent/tools/scheduled_tasks.py` | The `manage_scheduled_tasks` tool, whose `action` is create, list or cancel. |
| `agent/core.py` `scheduled_tasks_guidance` | The tool's guidance, written as an instructions hook so that a `bot_prompt` override keeps it. It is present only while `SCHEDULED_TASKS_ENABLED` is on, and never in a scheduled run. |
| `streams/scheduler.py` | The fourth stream-worker task. It claims due rows, advances them, hands each run over, and sweeps orphans. |
| `streams/scheduled_runner.py` | The run rules (send-time checks, outcomes, auto-disable) and the agent phase of an agent task. |
| `streams/scheduled_sender.py` | The fifth stream-worker task. It sends `ready` runs on a paced Baileys lane and a Telegram/Cloud lane. |
| `streams/consumer.py` | Hands a `trigger=scheduled` stream entry to the agent phase instead of the chat processor. |
| `routes/scheduled_tasks.py` | `/admin/scheduled-tasks` |

## Kinds

- **`reminder`** sends `⏰ <text>`, with the text limited to 1000 characters, and makes no LLM call. The text is run through `markdown_to_whatsapp` when the task is created, because it is model output.
- **`agent`** runs `<text>` (up to 2000 characters) as an instruction and sends the result.
  - **Session:** a fresh one, with no chat history; core memory and the system prompt still apply.
  - **Prompt:** built by `scheduled_runner.agent_prompt` as the user turn. The fire time is on the last line so that the stable text caches. The prompt says to answer exactly `[SILENT]` when there is nothing new.
  - **Nothing to send:** a reply that contains the token, or an empty reply, sends nothing and records `silent`.
  - **Tools:** `AgentDeps.scheduled_run=True` makes the agent-wide `prepare_tools` (`agent/core.py` `scheduled_run_tools`) keep only the read-only `SCHEDULED_RUN_TOOLS`. That excludes anything that sends (it would bypass the run's checks), changes settings or memory, or schedules more tasks; the last is a recursion guard that the prompt also states.
  - **Group tools:** `is_group_admin` is None, so group-changing tools would refuse anyway.
  - **Model errors:** a model error records `failed/model_error` and sends nothing, so there is no unsolicited "Sorry".
  - **Formatting:** `---` burst lines become blank lines, because send-text doesn't split them.
  - **Frequency:** an agent schedule may fire at most 4 times a day (`AGENT_MAX_DAILY_FIRES`).

## Schedule grammar (`services/schedule.py`)

The model passes the user's words in this grammar and never computes a date.

| Kind | Forms |
|---|---|
| Once | `in 30m`, `in 2h`, `in 1d`, `in 1h30m`, `in 1h30` |
| | `at 18:30` (the next one) |
| | `today\|tomorrow at 9:00` |
| | `monday at 9:00` (the next Monday; `next monday` said on a Monday means a week out) |
| | `2026-11-20 09:00` / ISO (a naive time is local) |
| Repeating | `every 2h` |
| | `every day at 9:00`, `daily at 9`, `every weekday at 8:30`, `every weekend at 10` |
| | `every monday,friday at 9:00` |
| | a 5-field cron |

**Other rules:**
- Times accept `9`, `9:30`, `9am`, `9pm`, `21h` and `9h30`.
- Named forms compile to cron, so weekdays use cron numbering only (Sunday = 0).
- A cron that never fires (`0 9 30 2 *`) is refused.
- A one-shot must be in the future and within a year.
- `next_fire` skips the second pass of a DST fall-back hour, so a daily `30 1 * * *` in New York fires once on that day.

**Frequency limits** are measured over 8 days starting from the task's first slot, not from now. Otherwise `* * 1 * *`, which fires every minute on the 1st, would pass every check while it is more than 8 days away.

**Time zone:** `BOT_TIMEZONE` (hot), falling back to `BROADCAST_TIMEZONE` when empty, and never the server's zone. It is snapshotted on the task at creation (`scheduled_tasks.timezone`), so a change applies only to new tasks. Cron slots follow DST in that zone.

## Run states

```
reminder:  ready ──────────────────────────► sending ─► sent | failed | unknown
agent:     dispatched ─► running ─► ready ─► sending ─► sent | failed | unknown
                                 └► silent | failed
any state before sending ─► skipped
```

**Every state change is conditional on the state the step owns.** Claims are a conditional `UPDATE`, and outcomes go through `scheduled_runner.record`, which locks the run and checks its status. So an outcome that arrives after the orphan sweep settled the run is dropped, never written over it.

## Firing (`streams/scheduler.py`, every 15 s)

1. **Claim due rows.** The scheduler runs `SELECT … FROM scheduled_tasks WHERE status='active' AND next_run_at <= now … FOR UPDATE SKIP LOCKED`, 50 rows per batch, so several workers never take the same row.
   - A full batch is followed at once by the next one, up to 10 batches per tick.
   - Each task advances in its own savepoint. A task that raises is disabled (`advance_error`) instead of stalling every tick.
2. **Advance and record, in the same commit.** `next_run_at` moves to the first slot after now (NULL for a one-shot), and the slot's `task_runs` row is inserted.
   - The insert runs in a savepoint, so a slot that already has a run is never fired again.
   - **At most once:** a crash after the commit loses a slot; it never doubles one.
   - A recurring task with no next slot is disabled (`no_next_slot`).
3. **Downtime.** A recurring slot fires only if it is less late than its catch-up window: half its period, clamped to between 2 min and 2 h. Otherwise it is recorded `skipped/missed`.
   - The next slot is always the first after now, so N missed slots never become N sends.
   - A one-shot fires up to 2 h late (`ONE_SHOT_GRACE`). Later than that, it is `skipped/missed` and the task is `disabled` with `disabled_reason=missed`, so it shows up in `/admin` instead of vanishing.
4. **Hand over.**
   - A reminder's run is created `ready`, with `message = "⏰ " + text`. It never goes through the chat stream: it has no LLM call and needs no ordering against the chat's messages.
   - An agent task's run is created `dispatched`. Then `XADD stream:user:{chat id} {trigger: scheduled, run_id, user_id}` runs, one Redis pipeline per batch, so the agent run is serialised with that chat's messages.
   - If the pipeline fails, those runs are recorded `failed/enqueue_failed` at once. If that update also fails, the 1 h expiry below settles them.
   - **Pipeline note:** such an entry must never be merged into a message burst or deduplicated against a user message. A redelivered entry is harmless, because the claim makes it a no-op.
5. **Orphan sweep:**

   | Run state | Settled after | Becomes | Why |
   |---|---|---|---|
   | `dispatched` | 1 h | `failed/expired` | Stuck behind a dead stream; not sent hours late. |
   | `ready` | 1 h | `failed/expired` | No sender picked it up. |
   | `running` | 15 min | `failed/abandoned` | Crashed during the agent run; nothing was sent. |
   | `sending` | 15 min from the send claim | `unknown` | Crashed mid-send; never re-sent. |

6. **Turning it off.** `SCHEDULED_TASKS_ENABLED=false` stops firing, but the sweep still runs. When it is turned back on, a slot that fell due meanwhile fires only within its catch-up window. Older slots are recorded `missed`, and a missed one-shot is disabled.

## Agent phase (`streams/scheduled_runner.py`, in the chat consumer)

1. The runner claims the run `dispatched → running`. If the claim is lost (the run was settled, taken by another worker, or redelivered), it does nothing.
2. It runs the send-time checks below. A run that would be skipped never costs an LLM call.
3. It runs the agent. `[SILENT]` records `silent`, an error records `failed`, and anything else is stored as `ready` with the text.

The runner never sends and never waits on pacing. A paced send inside the chat consumer would hold its `asyncio.gather` over all streams, and with it every other chat's reply, for as long as the queue of Baileys sends took.

## Sending (`streams/scheduled_sender.py`)

The sender has two lanes:
- **Baileys:** one send at a time, 3-8 s apart, so many tasks due at 09:00 don't burst from one WhatsApp account.
- **Telegram and Cloud:** up to 10 sends at once.

Each lane works in this order:
1. **Claim.** It claims the oldest `ready` runs for its platforms with `FOR UPDATE OF task_runs SKIP LOCKED` and moves them to `sending`. `started_at` is set to now, which starts the orphan clock, and the change is committed.
2. **Re-check.** The checks run again; each failure is recorded as a `skipped` run:
   - the task is no longer active (`cancelled`);
   - the chat row is gone (`user_deleted`);
   - the whitelist (`not_whitelisted`, see Rules);
   - a Cloud chat is outside the 23 h window (`cloud_window`; templates are not implemented, and the tool warns about this at creation);
   - the chat has been sent more than `SCHEDULED_TASKS_MAX_RUNS_PER_DAY` scheduled messages in 24 h (`daily_limit`).
3. **Deliver.** It uses the broadcast sender's `_deliver` (typing first on Baileys) on the task's own client (`client_id`). Telegram gets the 120 s timeout. There is no daytime window, because the user chose the time.
4. **Record the outcome** via `classify_outcome`:

   | Result | Recorded as | Next |
   |---|---|---|
   | Delivered | `sent`, with the text saved to the chat's history as an `assistant` message in the same commit | Recording is retried 3×; if it still fails, the run stays `sending` and is swept to `unknown`. |
   | 400 / 403 / 404 / 422 | `failed`, with the code | — |
   | 5xx, timeouts, `success:false` | `unknown` | Never retried. |
   | 503 and connect errors (provably nothing sent) | `failed` | A one-shot is re-armed 2 min later, 3 attempts in all. |

**Pacing is per worker process** and independent of the broadcast lane's pacing; the two don't share one spacing. A deployment with several worker processes would multiply the Baileys rate. The deployment has one worker; if that changes, a Redis lease like `broadcast:lock` would fix it.

## Auto-disable

- **What counts:** only real failures (`failed`, `unknown`) count toward `consecutive_failures`. At 3 the task is `disabled` with `disabled_reason=consecutive_failures`.
- **What doesn't:** skips (`cloud_window`, `daily_limit`, `not_whitelisted`, `user_deleted`, `missed`) only set `last_status` / `last_error`. A Cloud user who is quiet for three days keeps their reminder, and it resumes once they write again.
- **One-shots:** a one-shot ends `completed` when it is delivered (or silent), and `disabled` with the reason otherwise, skips included.

## Rules

- **Delivery target:** a task always delivers to the chat it was created in: `user_id` is that chat's row and `address` is its JID, both from `AgentDeps`. A group's task goes only to that group, and a private task never goes to a group.
- **Group permissions:** in a group, `create` and `cancel` need `is_group_admin is True` (`_group.group_admin_refusal`, which fails closed). Any member may `list`. `cancel` takes the 8-character id prefix and matches only this chat's active or disabled tasks, filtered in SQL.
- **Whitelist at send time:** the run uses the chat's own rule, `whitelist.chat_in_scope`, which `routes/chat.py` `_is_whitelisted` also uses, on the address the chat route checked.
  - Under `GROUP_GATING=membership` that rule admits every group. So a group task also needs its creator, `created_by_jid`, to still be whitelisted.
  - `created_by_jid` is the message's `sender_jid`, read through `AgentDeps.user_message_id`. On Baileys it is the participant's phone JID, the value the client's sender check uses; on Telegram it is `tg:<id>`.
  - A group task with no recorded creator fails closed.
- **Limits** (all hot):
  - `SCHEDULED_TASKS_MAX_PER_CHAT`: active tasks per chat (10). An admin re-enable is also refused at the limit.
  - `SCHEDULED_TASKS_MIN_INTERVAL_MINUTES`: the shortest time between two runs of a recurring task (60).
  - `SCHEDULED_TASKS_MAX_RUNS_PER_DAY`: scheduled messages per chat per 24 h (24).
- **Cleanup:** `/clean all` cancels the chat's active and disabled tasks. Deleting a chat row (a `/link` orphan) deletes its tasks and runs (`ON DELETE CASCADE`).

## `/admin/scheduled-tasks`

- **`GET`** takes the filters `status`, `kind`, `limit` (≤200) and `offset`. Results are ordered by `updated_at desc` and include `counts_by_status`.
- **`GET /{id}`** adds `cron_expr`, `interval_seconds`, `run_at` and the last 20 runs. It is rate-limit exempt.
- **`PATCH /{id}`** takes `{status: active|cancelled}`. Re-enabling resets the failure count and arms the next slot after now. It returns 409 for a one-shot whose time has passed, for a chat already at `SCHEDULED_TASKS_MAX_PER_CHAT` active tasks, and for a completed or cancelled task.
- **`DELETE /{id}`** deletes the task.
- **`GET /admin/overview`** has `disabled_scheduled_tasks`.

**FleetView contract:** `ScheduledTaskStatus`, `ScheduledTaskKind` and `TaskRunStatus` become closed FleetView enums once it mirrors them. `last_error`, `error_code` and `disabled_reason` are open strings.

## Migrations

Both tables are new, so `create_all()` creates them with all their columns and indexes, including `task_runs.message`, `scheduled_tasks.created_by_jid` and the partial index `ix_task_runs_open`. That holds only while they ship with the tables. A later column or index on either table needs a migration file like any other (`docs/migrations/README.md`).

## Verified against real Postgres

These ran against a real Postgres (`pgvector/pgvector:pg16`):
- **Claiming:** 4 concurrent `claim_due` calls over 40 due tasks produced 40 runs, one per task.
- **No double fire:** a second tick fires nothing, and a task re-armed onto a slot that already ran fires nothing and moves on.
- **Claim races:** 4 concurrent `claim_ready` calls on one `ready` run gave 1 winner, and 8 concurrent agent-phase claims gave 1 winner.
- **Delivery:** a reminder went `ready → sending → sent`, with its text saved to history.
- **Late outcomes:** a late `sent` after the sweep's `unknown` is not recorded.
- **Agent path:** an agent run went `dispatched → running → ready` and was not visible to the Baileys lane. A redelivered entry was a no-op.
- **Cascade:** deleting the user row removes its tasks and runs.
- **Indexes:** `create_all()` creates `ix_scheduled_tasks_due`, `ix_task_runs_open` and `uq_task_runs_slot`.
