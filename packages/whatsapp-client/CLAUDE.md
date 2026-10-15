# whatsapp-client (Baileys)

Loaded on demand when working in this folder. The root `CLAUDE.md` holds the architecture, commands, security model and cross-cutting gotchas. The end-to-end Baileys message flow is in the root file.

## Gotchas

- **WA Web version must be fetched, never pinned**: Baileys bundles a hardcoded WhatsApp Web version that goes stale within months; once WhatsApp rejects it, pairing dies with `Connection Failure` / `statusCode: 405` at the handshake, before any QR is emitted (existing sessions keep reconnecting). `services/wa-version.ts` `getWaVersionConfig()` fetches the live version via **`fetchLatestWaWebVersion()`**. Prefer it over `fetchLatestBaileysVersion()`, which returns Baileys' bundled constant while reporting `isLatest: true` — fine until it rots, which is the whole failure mode. Do NOT hardcode a version tuple: a pinned tuple is what left the forks broken. Only `fetchLatestWaWebVersion` forwards a `signal` to `fetch`; both *resolve* (never reject) on failure, handing back the stale bundled version, so branch on the returned `error` field. Successful fetches are cached per process (`initializeWhatsApp()` is also the reconnect path); failures are not, so they retry
- **NEVER pass `version: undefined` to `makeWASocket`**: Baileys merges config as `{ ...DEFAULT_CONNECTION_CONFIG, ...config }`, so an explicit `undefined` key *overwrites* its bundled default instead of falling through to it. `getUserAgent()` then does `config.version[0]` and throws `TypeError`, which surfaces as a non-Boom close event (`statusCode` is `undefined`, so no 405 branch fires) and retries forever. Omit the key entirely — that's why `getWaVersionConfig()` returns a spreadable `{ version?: WAVersion }` fragment (`{}` on failure) rather than `WAVersion | undefined`. Same trap for any other optional `makeWASocket` option
- Must call `normalizeMessageContent()` before type-checking any message — unwraps viewOnce/ephemeral wrappers
- `contextInfo` (mentions, quoted messages) is nested under specific message types (`imageMessage.contextInfo`, `audioMessage.contextInfo`, etc.) — NOT only on `extendedTextMessage`
- Bot identity uses two formats: JID (`@s.whatsapp.net`) and LID (`@lid`) — both must be checked for mentions/replies (see `utils/message.ts`)
- Group admin check is lazy: only for addressed group messages (`resolveSenderGroupAdmin` in `services/group-admin.ts`, bounded at 3s) — a live `groupMetadata` fetch for slash commands, the 5-min `group-cache.ts` entry for other addressed messages (group-changing agent tools need it; promote/demote evict the entry), never for saved-only chatter. `isSenderGroupAdmin` (`utils/message.ts`) matches `key.participant`/`participantAlt` against each member's `id`/`lid`/`phoneNumber` — comparing only `id` to `participant` missed admins whenever LID vs phone addressing differed, and the API now fails closed on anything but `true`
- **Group `sender_jid` is the participant's PHONE JID** (`resolveParticipantJid` in `utils/jid.ts`: direct phone JID → `key.participantAlt` → LID↔PN store, falling back to the device-stripped raw LID), so one human is recorded under one identity regardless of LID or phone addressing. It reuses `resolveSenderPhone`, so an unresolvable LID logs the same warnings
- Only PDF documents are accepted for processing; other types return a user-facing error message
- **The whitelist check must run after `resolveSenderPhone()`**: under Baileys v7 a chat is usually LID-addressed, so `msg.key.remoteJid` is `…@lid` — an anonymized account id whose digits are NOT a phone. Comparing those digits to a phone entry is why a documented bare-phone whitelist silently matched nothing. The check now sits in `whatsapp.ts` *between* identity resolution and content handling, which is a narrow window on both sides: it must be **after** `resolveSenderPhone` (no phone before it) and **before** `sock.user!.id` — if `sock.user` is unset that dereference throws into the per-message `catch`, which calls `sendFailureReaction`, i.e. it would react into a chat that was just blocked. It also stays ahead of `transcribeAudioMessage` / `extractImageData` / `extractDocumentData` / `groupMetadata`, so a blocked message still costs nothing. Enforced by `tests/unit/whatsapp-whitelist-placement.test.ts` — the ordering is invisible to every other test, so without it the gate could silently drift back above `resolveSenderPhone` and restore the original bug
  - Two caveats on "a blocked message costs nothing". It holds only for the **client-side** filter: in the admin-managed mode above (client `WHITELIST_PHONES` empty, enforcement at the AI API), the 403 lands *after* Cloud has sent read receipts + typing and downloaded or transcribed the media. And `resolveSenderPhone` now runs for blocked messages too — cheap (in-process LRU or one local keystore read, no network), but each blocked LID miss emits a `logger.warn`/`logger.error` from `utils/jid.ts`, so a spam wave against a tight whitelist floods the logs at warn/error
- **An unresolvable LID fails closed**: if `key.remoteJidAlt` is absent *and* the LID↔PN store has no mapping (a freshly-paired session), `resolveSenderPhone` returns `undefined` and a phone-whitelisted contact is dropped until the mapping warms. Hosted LIDs (`@hosted`) never resolve at all. The drop logs `phoneResolved: false`; the escape hatch is to whitelist the raw `…@lid` verbatim


## Adding a new message handler (whatsapp-client)
1. Create handler in `src/handlers/` as a pure async function — follow the pattern in `text.ts`
2. Wrap in try/catch: use `sendFailureReaction(sock, msg)` + `logger.error` in catch, `sendPresenceUpdate('paused')` in finally
3. Register it in `src/whatsapp.ts` inside the `messages.upsert` event handler
4. Add any new routes in `src/routes/` with Zod schemas in `src/schemas/`
5. Add unit tests in `tests/unit/` and integration tests in `tests/integration/` following existing patterns


## Adding a WhatsApp media route (multipart — whatsapp-client)
Multipart routes can't use Zod validation directly. Follow the pattern in `routes/media.ts`:
1. Use plain JSON Schema for `schema.body` (not Zod)
2. Add custom `validatorCompiler: () => (data) => ({ value: data })` to bypass automatic validation
3. Extract fields from `request.body` — **multipart form fields are `{ value: string }` objects**, not raw strings
4. Validate files with `validateMediaFile()` from `utils/file-validation.ts`
5. Get socket via `getBaileysSocket()` from `services/baileys.ts`
