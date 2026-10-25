---
name: handler
description: >-
  Scaffolds new WhatsApp message handlers for the Baileys client with media
  extraction, typing indicators, error handling, and whatsapp.ts registration.
  Use when adding support for a new message type in whatsapp-client.
---

# Add WhatsApp Message Handler (`handler`)

Guide for adding new message type handlers to `packages/whatsapp-client/` (Baileys WebSocket).

## Pattern A: Media Extraction Handler

For binary message types (images, documents, audio):
Location: `packages/whatsapp-client/src/handlers/`

```typescript
import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { downloadMediaMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

export interface MyMediaData {
  buffer: Buffer;
  mimetype: string;
}

export async function extractMyMediaData(
  sock: WASocket,
  msg: WAMessage
): Promise<MyMediaData | null> {
  const mediaMessage = msg.message?.myMediaMessage;
  if (!mediaMessage) return null;

  try {
    const buffer = await downloadMediaMessage(
      msg,
      'buffer',
      {},
      {
        logger: logger.child({ module: 'baileys-download' }),
        reuploadRequest: sock.updateMediaMessage,
      }
    );

    if (!buffer) {
      throw new Error('Failed to download media');
    }

    const mimetype = mediaMessage.mimetype || 'application/octet-stream';
    logger.info({ size: buffer.length, mimetype }, 'Media downloaded successfully');

    return {
      buffer: buffer as Buffer,
      mimetype,
    };
  } catch (error) {
    logger.error({ error }, 'Error downloading media');
    return null;
  }
}
```

## Pattern B: Processing Handler

Core processing funnels into `handleTextMessage()` in `packages/whatsapp-client/src/handlers/text.ts`.

### Lifecycle & Presence Invariants:
- Typing indicator: `await sock.sendPresenceUpdate('composing', jid)` at start.
- Wrap in `try/catch/finally`:
  - `catch`: Log error, send `sendFailureReaction(sock, msg)`, send fallback error message.
  - `finally`: ALWAYS reset presence via `await sock.sendPresenceUpdate('paused', jid)`.

## Registration in `whatsapp.ts`

Insert into the message loop in `packages/whatsapp-client/src/whatsapp.ts` before the final text check:

```typescript
// Handle custom media messages
if (normalizedMessage?.myMediaMessage) {
  if (saveOnly) {
    // Save to history without binary download
    const caption = normalizedMessage.myMediaMessage.caption;
    const marker = caption ? `[Media: ${caption}]` : '[Media]';
    await handleTextMessage(sock, msg, marker, undefined, undefined, { saveOnly: true });
  } else {
    const mediaData = await extractMyMediaData(sock, msg);
    if (!mediaData) {
      await sendFailureReaction(sock, msg);
      continue;
    }
    const prompt = normalizedMessage.myMediaMessage.caption || 'Default prompt';
    await handleTextMessage(sock, msg, prompt, mediaData);
  }
  continue; // Prevent falling through
}
```

## Baileys Gotchas & Invariants
- `normalizeMessageContent()` MUST be called before checking message properties (unwraps `viewOnceMessage` and `ephemeralMessage`).
- Bot mentions: Both JID (`@s.whatsapp.net`) and LID (`@lid`) formats must be checked (`utils/message.ts`).
- `saveOnly` handling: Non-mentioned group messages must only record history, never invoke the AI agent.
- Always `continue` after dispatching a message type to prevent duplicate processing.
