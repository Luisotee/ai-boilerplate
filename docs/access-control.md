# Access control: group admins, whitelist, group gating

Detail behind the rate-limiting, group-admin, whitelist and group-gating rules in the root `CLAUDE.md` / `AGENTS.md`. Read it before changing `commands.py` admin checks, `agent/tools/_group.py`, `utils/whitelist.ts`, `utils/gating.ts`, `ai_api/whitelist.py`, or any place that calls them. Platform specifics live in `packages/whatsapp-client/CLAUDE.md` (LID resolution, gate placement), `packages/whatsapp-cloud/CLAUDE.md` (no group context) and `packages/telegram-client/CLAUDE.md` (`tg:` entries).

## Rate limiting and API keys

`RATE_LIMIT_GLOBAL` (default 30/min), `RATE_LIMIT_EXPENSIVE` (default 5/min). On the TS clients, requests with a valid `X-API-Key` are exempt (`utils/api-key.ts` `hasValidApiKey`, shared with the auth hook), because all inter-service traffic comes from the AI API's single IP and a shared per-IP budget would 429, and lose, legitimate bot traffic in a burst. `hasValidApiKey` compares **byte** lengths before `timingSafeEqual`, so a multibyte header gets a 401 instead of throwing a 500. The auth hook always runs BEFORE the limiter (`@fastify/rate-limit` attaches route-level hooks, which Fastify runs last), so bad-key requests get a 401 without being counted: the limiter does **not** throttle API-key guessing. `/health*` (incl. `/health/ready`) stays exempt from both.

## Group admin commands fail closed

In a group, `/clean`, `/tts`, `/stt`, `/settings`, `/memories` (and `/broadcast on|off`, gated in its handler) run only when the client sends `is_group_admin: true`; `commands.py` checks `is_group_admin is not True`. The old `is False` check let an omitted field through, and the Telegram client never sent it, so any group member could `/clean all` a group.

- Baileys resolves admin status lazily from `groupMetadata` (`isSenderGroupAdmin` in `utils/message.ts`, matching `participant`/`participantAlt` against each member's `id`/`lid`/`phoneNumber`).
- Telegram resolves it via `getChatMember` (`services/group-admin.ts`).
- A lookup failure means not-admin. Cloud API webhooks carry no group context.

Agent tools that change a whole group's state apply the same rule through `agent/tools/_group.py` `group_admin_refusal` (a group conversation JID needs `AgentDeps.is_group_admin is True`): `clean_user_data`, the `settings` tool's `tts`, `stt` and `announcements` actions, and the `memory` tool when its result is destructive (`is_destructive_rewrite` in `agent/tools/memory.py`: empty, or less than half of the previous document's line characters survive verbatim). Additive and small edits stay open to every member, so a non-admin can't wipe a group's memory by rewriting it to `"x"`. `/memories undo` (admin-only in groups, like every `/memories` subcommand) restores the backup in `core_memories.previous_content`. `core_memories.content_trusted` records whether the current document came from a trusted source: a private chat, a confirmed admin, `/memories clear` or undo (`is_trusted_sender` in `_group.py`). A trusted save backs up the document it replaces; any other group member's save backs it up only when it is trusted (or there is no backup), and an empty document never replaces the backup (`backup_after_save`). So the backup is always the last trusted state: erosion by members (filler appended, then everything else cut, each step passing the guard), a save after a mistaken `/memories clear`, and repeated erosion after an undo all stay undoable, and undo drops the members' edits made since. Without this, any member could ask the bot in plain words to do what the commands reserve for admins.

## User whitelist (`WHITELIST_PHONES`)

`WHITELIST_PHONES` is comma-separated; empty means every user is allowed (the gate is disabled). When it is set, non-whitelisted messages are silently ignored. It is checked at the WhatsApp client level (primary) and again at the AI API level (defense in depth).

**Parsing.** Each entry matches **either as a phone number or as a verbatim chat id**. `utils/whitelist.ts` (TS ×3, kept byte-identical by `tests/unit/whitelist-copies.test.ts`) and `ai_api/whitelist.py` parse it into two sets, matched by `isWhitelisted` / `is_whitelisted`. The Python side is a pure mirror imported by `routes/chat.py` (which reads the effective value via `runtime_config`) and `routes/admin.py` (which validates a proposed one). It must never be *more permissive* than the TS gate it backs up, which is why both spell out the same 29-codepoint separator class instead of using `\s`.

An entry is first stripped of a device suffix (`:50@` → `@`) and a trailing `@s.whatsapp.net`. If what remains still contains an `@`, or the entry started with `tg:`, it is kept **verbatim only**, so `tg:-100…` can never be mistaken for a phone. Otherwise it is normalized to bare digits (tolerating `+`, spaces, `-`, `()`, `.`) and stored in the phone set. Entries below 5 digits never become phones. Every entry is *also* kept verbatim, which keeps a bare `120363…` matching a group and a bare LID matching its chat on existing installs.

**Matching rules.**
- **Normalized digits are consulted only for `@s.whatsapp.net` JIDs.** A phone entry must not admit an `@lid`/`@g.us`/`@broadcast` chat that merely shares its digits. A *bare* entry stays namespace-blind, though: that is the pre-existing `jid.split("@")[0] in whitelist` behaviour and the same code path that keeps bare `120363…` working, so it is pinned by a test rather than "fixed".
- **The `phone` argument is never consulted for a group JID**, so a member's whitelisted phone never admits the whole group. That is narrower than "a group is matchable only by its `…@g.us` JID": a legacy bare entry equal to the group's id still admits it.
- Matching is exact. There is deliberately **no** "last N digits" fuzzy fallback, because it would let `…5945319` whitelist arbitrary countries. If a BR/MX-style number variant (`55DD9XXXXXXXX` vs `55DDXXXXXXXX`) fails to match, read the `phone` value from the client log and paste that.
- Accepted compat break from the two-set split: a bare `<digits>:<device>` entry (a device suffix with no `@`) no longer matches, because the matcher strips the suffix off the incoming jid before comparing local parts. Use the full JID or the bare number.

**The AI-API layer** matches on the client-supplied `phone` field as well as `whatsapp_jid`. It must, because a Baileys chat is usually LID-addressed (see the LID gotcha in `packages/whatsapp-client/CLAUDE.md`). That is not new trust: the route is already behind `X-API-Key`, and `get_or_create_user` already trusts the same field for identity. The client-side check remains primary.

**The clients read the whitelist live from the AI API.** Each TS client gates on the AI API's effective value (`GET /chat/whitelist` in `routes/access.py`: the `/admin` override, else the API's `WHITELIST_PHONES`), fetched by `services/live-whitelist.ts` (byte-identical ×3, pinned by `whitelist-copies.test.ts`) and parsed with the unchanged `utils/whitelist.ts`. So a `PATCH /admin/settings` addition or removal reaches every client within about 20 s (the API's ~10 s `runtime_config` cache plus the client's 10 s cache), with no env edit or restart. The clients' own `WHITELIST_PHONES` env is **no longer read**: a client that used a stricter `.env.local` value now follows the API's value, so keep the whitelist in the root `.env` or `/admin`. The route is `@limiter.exempt` (polled every ~10 s per client) and behind `X-API-Key`.

Fail-closed paths of the live whitelist:
- the fetch fails (network error, timeout of `FETCH_TIMEOUT_POLLING_MS`, non-2xx, a body without a string `whitelist_phones`) → `getLiveWhitelist()` returns `null` and the client drops the message: not saved, not answered, no reaction. This holds even when the whitelist is disabled, because the client can't know that without the API. Logged at warn at most once a minute;
- a failure is cached for 2 s, so an outage costs one fetch per 2 s, not one per message;
- a good value is served only within its 10 s TTL; there is no stale or env fallback after it expires;
- concurrent messages share one in-flight fetch.

Every message needs the AI API to be saved or answered anyway, so failing closed loses nothing a working API would have kept. The AI-API layer (`_is_whitelisted` in `routes/chat.py`) still checks again; `streams/` and `queue/` never evaluate the whitelist.

`PATCH /admin/settings` **rejects a whitelist that parses to zero entries** (`""`, `" "`, `","`). An override shadows the env value across restarts, so clearing the field would otherwise disable the gate for good. `DELETE /admin/settings/whitelist_phones` is the way to revert to the env value.

## Group gating (`GROUP_GATING=jid|membership`, default `jid`)

`GROUP_GATING` selects how a *group* gets in scope when `WHITELIST_PHONES` is set. It is read by the AI API (`config.py`, display-only in `runtime_config`) and by the Baileys and Telegram clients (`config.groupGating`); keep them equal via the root `.env`. The truth table is `utils/gating.ts` (`gateMessage` / `decideGroupGating`), byte-identical in whatsapp-client and telegram-client (enforced by `whitelist-copies.test.ts`). In `jid` mode the callers pass `groupAllowed = groupExplicit`, which reproduces the historical gate exactly.

- `jid`: a group is admitted only by its own id; then every member may address the bot.
- `membership` (Baileys): a group is in scope if it is listed, the sender is whitelisted, or ANY participant is whitelisted (`groupHasWhitelistedMember` in `services/groups.ts`: participant `id`/`lid`/`phoneNumber` through the whitelist matcher, then a quiet `getPNForLID` fallback for LID-only participants; metadata from `group-cache.ts`, bounded at 10s, **fails closed**). The bot saves every message of an in-scope group but replies only to a whitelisted sender (`gate.suppressResponse` → `saveOnly`), unless the group is listed explicitly. The gate still runs between `resolveSenderPhone` and `sock.user!.id` (`resolveGate` in `whatsapp.ts`; pinned by `whatsapp-whitelist-placement.test.ts` for both modes).
- `membership` (Telegram): the Bot API cannot enumerate members, so every group the bot is in is in scope and saved. Replies are gated per sender (`tg:<from.id>`) unless the group id is listed (`resolveGate` in `updates.ts`).
- AI API: in `membership` mode `_is_whitelisted` admits every group JID (`is_group_jid`: `@g.us` or `tg:-…`), because only the client can see membership, so it is authoritative for groups. 1:1 chats are enforced in both modes.
