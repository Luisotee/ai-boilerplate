# OpenAI-compatible API and terminal chat

The bot can run in web, desktop and phone apps that speak the OpenAI chat API (Open WebUI, LibreChat, Chatbox, …) without a second pipeline. `POST /v1/chat/completions` maps onto the same enqueue → stream worker → job path as WhatsApp. For testing without WhatsApp there is also a terminal chat. Code: `routes/openai_compat.py`, `services/api_keys.py`, `services/chat_jobs.py`, `surfaces.py`, `scripts/api_keys.py`, `scripts/chat.py`.

## Keys

Every app user (or integration) has its own key, mapped to one `users` row. The internal `AI_API_KEY` is never accepted on `/v1`.

```bash
docker compose exec api python -m ai_api.scripts.api_keys create --name "Ana web"
docker compose exec api python -m ai_api.scripts.api_keys create --name "Ana" --jid 5511999999999@s.whatsapp.net
docker compose exec api python -m ai_api.scripts.api_keys list
docker compose exec api python -m ai_api.scripts.api_keys revoke sk-AbCdEfG   # prefix from `list`, or the id
```

- **Issued** by the operator only. `create` prints the key once (`sk-` + 256 random bits). Without `--jid` it creates a new private user `web:<id>`; with `--jid` the key belongs to an existing private WhatsApp/Telegram chat.
- **Stored** as its sha256 in `api_keys` (plus a 10-character prefix for listings). A new table, so `create_all()` makes it; no migration.
- **Revoked** with `revoke`: `revoked_at` is set and checked on every request, so it takes effect at once.
- **Linked** with `/link`: in the app, send `/link`, then `/link <code>` on WhatsApp or Telegram (or the other way round). The WhatsApp/Telegram row is kept, the `web:` row and its history are discarded, and the key moves to the kept row. From then on the app shares that chat's history, core memory and preferences. A WhatsApp row already linked to Telegram can still take a web link. To detach a key, revoke it (`/unlink` only concerns Telegram). The WhatsApp↔Telegram merges (`/link`, `/linkphone`) move a key attached to the Telegram orphan too.
- `WHITELIST_PHONES` does not apply to `/v1`: an issued key is the grant. Linking can't widen access, because both `/link` steps on WhatsApp/Telegram go through `/chat/enqueue`'s whitelist.

## Endpoints

`Authorization: Bearer sk-…` on both. `APIKeyMiddleware` skips only paths starting with `/v1/`; the router's `require_app_key` dependency authenticates every `/v1` route (a test asserts it). Errors use OpenAI's shape `{"error": {"message", "type", "code"}}`.

| Method | Path | |
|---|---|---|
| GET | `/v1/models` | One model, id `OPENAI_API_MODEL_ID` (default `ai-agent`) |
| POST | `/v1/chat/completions` | Answers the last user message as the bot |

**Request**: the OpenAI body. Only `messages[-1]`, `stream` and `stream_options.include_usage` are used; `model`, `temperature`, `tools` and the rest are accepted and ignored. Content may be a string or text parts. Image/file/audio parts are a 400 (text only for now). Max 32,000 characters.

**One message per request.** The server keeps the conversation, so only the last message counts and it must have `role: "user"` (else 400). The client's earlier messages and its system prompt are ignored; the bot's own system prompt applies.

- Open WebUI, LibreChat and Chatbox resend the whole transcript every turn: harmless, the tail is read.
- *Regenerate* or *edit and resend* sends the last user message again, so the server history gets that turn twice. Branches in the client don't fork the server history.
- A `/link` merge while a web reply is still running: the worker's save recreates an empty `web:` row (no key, no route) holding that last turn. Harmless and rare (the code must be redeemed during a pending reply); the same race exists for WhatsApp↔Telegram merges.
- Every chat in the app shares one server conversation per key: it is one person's memory, the same one WhatsApp uses once linked.

**Slash commands** (`/help`, `/settings`, `/memories`, `/clean`, `/link`, …) run as on `/chat/enqueue` and come back as the reply. `/unlink` is refused here: on a key attached to a WhatsApp user it would cut the WhatsApp↔Telegram link, not detach the app (revoke the key for that).

**Replies** keep the model's Markdown (`surfaces.py`: `client_id="web"`), minus the `---` burst lines, which would otherwise split a WhatsApp reply into several messages. The formatting guidance asks the model for Markdown. History stores the WhatsApp-markup version, so it stays uniform for a user linked across surfaces; only a web-only `web:` user keeps Markdown in history (`surfaces.stores_markdown`), since a `/link` discards that row and its history. Tools that deliver through a chat client (WhatsApp reaction/message/location/contact, shared-group tools) are hidden; so is the broadcast-subscription tool for a web-only user, who has no broadcast route.

**Non-streaming**: a `chat.completion` with `finish_reason: "stop"` and zero `usage` (token cost lives in Logfire). The route polls the job every 500 ms for up to 120 s, like the chat clients: a failed job is a 502, a timeout a 504 (the job still finishes and its answer lands in history).

**`stream: true`**: `text/event-stream` with a role delta at once, `: ping` comments every 15 s while the worker runs, the reply as one content delta (the worker publishes the reply in one piece), a `finish_reason: "stop"` chunk, a usage chunk if `stream_options.include_usage`, then `data: [DONE]`. A failure or timeout is an `{"error": …}` event before `[DONE]`.

**Rate limits** are per key, not per IP (one Open WebUI server proxies all its users from one address): `RATE_LIMIT_EXPENSIVE`/min for completions, `RATE_LIMIT_GLOBAL`/min for models. Those run after auth, and a route with its own limit skips the middleware's per-IP default, so failed key checks get their own limit: `RATE_LIMIT_GLOBAL`/min per IP, then 429 before the key lookup (valid keys don't count).

**DB connection**: the route closes its session once the turn is saved and queued, before the wait (up to 120 s), so waiting chats never hold pooled connections.

## Client setup

Base URL `https://<your-api>/v1`, API key `sk-…`, model from `/v1/models`. Turn off the clients' background chores. They would otherwise land in the user's conversation. The two recognisable ones are refused with a 400 (the client then keeps a default title):

- **Open WebUI**: Admin Settings > Interface: turn off Title, Tags and Follow-up generation and Autocomplete (or `ENABLE_TITLE_GENERATION=False`, `ENABLE_TAGS_GENERATION=False`, `ENABLE_FOLLOW_UP_GENERATION=False`, `ENABLE_AUTOCOMPLETE_GENERATION=False`). Its chore prompts start with `### Task:`, which `/v1` refuses.
- **LibreChat**: in `librechat.yaml`, set `titleConvo: false` on the custom endpoint. Its default title request is a single `system` message, which `/v1` refuses.
- **Chatbox**: turn off automatic topic naming in its settings. Its naming request is a normal user message that the server can't tell apart, so it would be answered and saved.

Browser-only clients calling the API directly need their origin in `CORS_ORIGINS`. Open WebUI and LibreChat call it from their server.

## Terminal chat

```bash
docker compose exec api python -m ai_api.scripts.chat --name luis   # name: [A-Za-z0-9_.-]
cd packages/ai-api && uv run python -m ai_api.scripts.chat      # local dev: dev:server + dev:queue running
```

It posts to `/chat/enqueue` as the private chat `cli:<name>` with `AI_API_KEY`, then polls `/chat/job/{id}`, so it exercises the worker, the model chain, the tools and the commands. Replies use WhatsApp markup, as a chat client would show them. `/quit` or Ctrl-D exits.

The API admits a `cli:` chat only from a loopback address, or when that exact `cli:<name>` is a `WHITELIST_PHONES` entry. An empty whitelist does not count. In Docker a call from the host arrives from the bridge network, so run the script inside the container as above, or whitelist the name. A reverse proxy on the same host that sends no `X-Forwarded-For` makes every request look local; `AI_API_KEY` is still required either way.

`web:` and `cli:` rows have no chat client: broadcasts skip them (`candidate_routes` returns none), and `_record_client` leaves their routing columns empty. In `/admin` they appear as ordinary private users with those JIDs.
