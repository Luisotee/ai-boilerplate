---
name: fleetview-check
description: Checks whether this branch's changes to the dashboard contract (admin/broadcast/bug-report/health/KB routes, schemas.py, the runtime_config REGISTRY) break FleetView, by asking a fresh read-only agent in the fleetview repo. Use after changing any of those files, and before opening or merging a PR that touches them.
---

# FleetView contract check

FleetView's Zod schemas mirror this repo's admin API strictly. A renamed or removed field, route or setting attribute (`hot` / `secret` / `choices`), or a new value in a closed enum (setting `type`, KB status, QR `status`, `conversation_type`, broadcast enums), breaks the dashboard for every bot. A new field is safe. Only fleetview knows its current schemas, so the verdict comes from an agent there.

## 1. Find the changed contract files

```bash
ROOT=$(git rev-parse --show-toplevel)
git -C "$ROOT" fetch -q origin main
CONTRACT=(
  packages/ai-api/src/ai_api/routes/admin.py
  packages/ai-api/src/ai_api/routes/broadcasts.py
  packages/ai-api/src/ai_api/routes/bug_reports.py
  packages/ai-api/src/ai_api/routes/health.py
  packages/ai-api/src/ai_api/routes/knowledge_base.py
  packages/ai-api/src/ai_api/schemas.py
  packages/ai-api/src/ai_api/runtime_config.py
)
git -C "$ROOT" diff --name-only origin/main...HEAD -- "${CONTRACT[@]}"   # committed
git -C "$ROOT" status --porcelain -- "${CONTRACT[@]}"                    # uncommitted
```

- **Nothing listed by either command:** report "No contract files changed; no FleetView check needed" and stop.
- **Any contract file listed as uncommitted** (whether or not others are committed): the check reads committed history only, so uncommitted contract edits would be missed. Ask the user to commit them first (or commit yourself if they asked you to), then run step 1 again.
- `runtime_config.py` counts only when the `REGISTRY` changed: look at its diff, and drop it from the list if the change is elsewhere in the file.

## 2. Ask a fresh fleetview agent

Run it from the fleetview checkout, with no `-c` (a fresh session) and with `--dangerously-skip-permissions` (headless mode needs it). It takes a few minutes, so give the command a 10-minute timeout. Use `agy -p` instead of `claude -p` only if the user asks for agy.

Run it as **one** shell command, exactly as below: shell variables from step 1 don't survive into a new tool call, so `ROOT` is computed again here, before the `cd`. Replace only the file list.

```bash
ROOT=$(git rev-parse --show-toplevel) && FILES="<the changed contract files from step 1, space-separated>" && \
cd /home/ubuntu/projects/fleetview && claude -p "Run: git -C $ROOT diff origin/main...HEAD -- $FILES. If that command fails or prints nothing, answer 'Unknown: empty or failed diff' and stop; never answer Safe without having read a diff. Otherwise: does it break a Zod schema in lib/bot-api/schemas.ts or a Server Action in lib/actions/? Read-only check: do not modify fleetview files, run write commands, or read .env* files. Answer Safe, Breaking or Needs a FleetView update, with file:line in both repos." --dangerously-skip-permissions
```

`$ROOT` is this checkout's (or worktree's) top level, so the fleetview agent diffs the branch you are on, not the main checkout.

## 3. Report

Give the verdict first (**Safe**, **Breaking**, **Needs a FleetView update** or **Unknown**), then the agent's file:line evidence.

- **Breaking:** propose the compatible change here (keep the old field or enum value, add instead of rename) before anything else.
- **Needs a FleetView update:** that change belongs in fleetview, made by an agent working there (`/delegate-claude` or a headless session in that repo), never by editing fleetview files from here. Say so and ask the user before starting it.
- **Unknown:** the diff didn't reach the agent. Check that `$ROOT` and the file list were filled in, then run step 2 again; never report it as Safe.
- Never edit fleetview files from this repo.
