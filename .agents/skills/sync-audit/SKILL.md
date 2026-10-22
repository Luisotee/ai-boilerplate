---
name: sync-audit
description: >-
  Audits feature, handler, route, and shared-utility parity across the TypeScript
  messaging clients (whatsapp-client, whatsapp-cloud, telegram-client). Use when
  adding a feature to one client or when asked to check client parity.
---

# Client Parity Audit (`sync-audit`)

Compare capabilities, handlers, routes, and shared utilities across the messaging client packages in `packages/`:

| Package | Protocol | Entry / Dispatch | Port |
|---------|----------|------------------|------|
| `packages/whatsapp-client/` | Baileys (WebSocket) | `src/whatsapp.ts` | 3001 |
| `packages/whatsapp-cloud/` | Meta Cloud API (HTTP webhooks) | `src/routes/webhook.ts` | 3002 |
| `packages/telegram-client/` | grammY (Webhook / Polling) | `src/updates.ts` | 3003 |

## Audit Checklist

1. **Shared Utilities Parity**:
   - `src/utils/whitelist.ts` (must remain byte-identical across all 3 TS clients and mirrored in `packages/ai-api/src/ai_api/whitelist.py`; verified by `whitelist-copies.test.ts`).
   - `src/utils/gating.ts` (must remain byte-identical across `whatsapp-client` and `telegram-client`).
   - `src/utils/api-key.ts` (`hasValidApiKey` byte-length check before `timingSafeEqual`).
2. **Handlers & Message Types**:
   - Compare `src/handlers/*.ts` across clients (text, audio/voice, image/photo, document/PDF).
   - Verify `saveOnly` handling for non-mentioned group messages (`whatsapp-client` and `telegram-client`), `is_group_admin` resolution, and failure reactions.
3. **Outbound & Admin Routes**:
   - Compare `src/routes/*.ts` (`messaging.ts`, `health.ts`, `webhook.ts`, `qr.ts`) and registration in `src/main.ts` + `tests/helpers/fastify.ts`.
4. **Known Intentional Differences (Do Not Flag as Bugs)**:
   - **Typing indicators**: Baileys uses WebSocket `sendPresenceUpdate('composing'/'paused')`; Cloud API uses Graph API `typing_indicator` on mark-as-read (`paused` is a no-op); Telegram uses `sendChatAction('typing')`.
   - **Group support**: Cloud API webhooks carry no group context; Baileys and Telegram support groups, `GROUP_GATING`, and `is_group_admin`.
   - **24-hour window**: Cloud API enforces a 24h customer-service window (`cloud_last_inbound_at`).
   - **Formatting**: AI API outputs WhatsApp markup; Telegram client further splits on `---` bursts and converts to Telegram HTML (4096-char limit).
