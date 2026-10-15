# whatsapp-cloud (Meta Cloud API)

Loaded on demand when working in this folder. The root `CLAUDE.md` holds the architecture, commands, security model and cross-cutting gotchas.

## Cloud API Message Flow (whatsapp-cloud)

1. Meta sends webhook POST → `routes/webhook.ts` verifies HMAC-SHA256 signature
2. Extract messages from `entry[].changes[].value.messages[]`
3. Type dispatch: text → `handlers/text.ts`, audio → `handlers/audio.ts`, image → `handlers/image.ts`, document → `handlers/document.ts`
4. Handlers download media via Graph API (`services/graph-api.ts`), convert phone→JID for AI API compatibility
5. Same AI API flow as Baileys: `api-client.ts` sends POST `/chat/enqueue` (includes `client_id: "cloud"` for routing) → polls for result
6. AI API resolves `client_id` to a pre-configured URL and routes callbacks to the correct client
7. Responses sent via Meta Graph API (`POST graph.facebook.com/{phone_number_id}/messages`)

## Gotchas

- **24-hour messaging window**: Can only send free-form messages within 24h of customer's last message — outside this window, template messages are required (not implemented)
- **Typing indicators**: Cloud API supports typing via the mark-as-read endpoint with `typing_indicator: { type: 'text' }` — auto-dismisses after 25s or on first outbound reply (whichever is first). Fired from `routes/webhook.ts` before the type-dispatch switch so media messages (audio/image/document) show typing before Graph download or transcription starts. Meta limits it to **one-shot per inbound wamid** — cannot be refreshed mid-response (no typing between multi-burst chunks; no `audio`/"recording" type). Default Graph API version is `v23.0`; `paused` state is a no-op
- **No message edit/delete**: Cloud API doesn't support editing messages; deletion is supported but not implemented — `operations.ts` routes return 501 for both
- **Media URL expiry**: Downloaded media URLs from Graph API are temporary — `downloadMedia()` fetches URL and downloads immediately in one call
- **Webhook routes exempt from API key auth**: `/webhook` GET/POST use HMAC signature verification via `META_APP_SECRET` instead
- **Phone ↔ JID translation**: Cloud API uses plain phone numbers, AI API expects JIDs — conversion happens at the Cloud client boundary via `utils/jid.ts`
- **client_id routing**: Each client sends a `client_id` (`"baileys"` or `"cloud"`) in enqueue requests — the AI API maps this to a pre-configured URL (`WHATSAPP_CLIENT_URL` / `WHATSAPP_CLOUD_CLIENT_URL`) to route callbacks
- **Whitelist group JID limitation**: `WHITELIST_PHONES` supports group JIDs (e.g. `120363...@g.us`) only on the Baileys client. The Cloud API webhook payload does not include group context — only the individual sender's phone number — so group JID entries in the whitelist have no effect for Cloud API messages. Cloud does accept every phone entry format (`+49…`, spaced, `…@s.whatsapp.net`) — `passesWhitelist` in `routes/webhook.ts` feeds the matcher both `phoneToJid(senderPhone)` and `+<senderPhone>`


## Adding a new message handler (whatsapp-cloud)
1. Create handler in `src/handlers/` as a pure async function — follow the pattern in `text.ts`
2. Wrap in try/catch: use `graphApi.sendReaction(senderPhone, messageId, '❌')` + `logger.error` in catch
3. Register in `src/routes/webhook.ts` inside the message type dispatch switch
4. Use `jidToPhone()` / `phoneToJid()` from `utils/jid.ts` when crossing API boundaries
5. Download media via `graphApi.downloadMedia(mediaId)` instead of Baileys `downloadMediaMessage()`
6. Add unit tests in `tests/unit/` and integration tests in `tests/integration/` following existing patterns
