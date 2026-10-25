---
name: cloud-handler
description: >-
  Scaffolds WhatsApp Cloud API message handlers with Graph API media download,
  error handling, and webhook.ts registration. Use when adding support for a new
  message type in whatsapp-cloud.
---

# Add Cloud Message Handler (`cloud-handler`)

Guide for adding new message type handlers to `packages/whatsapp-cloud/` (Meta WhatsApp Cloud API).

## Step 1: Create Media Extraction Handler

Location: `packages/whatsapp-cloud/src/handlers/`

```typescript
import * as graphApi from '../services/graph-api.js';
import { logger } from '../logger.js';

export async function extractMyMediaData(
  mediaId: string
): Promise<{ data: string; mimetype: string } | null> {
  try {
    const { buffer, mimetype } = await graphApi.downloadMedia(mediaId);

    logger.info({ mediaId, mimetype, size: buffer.length }, 'Media downloaded from Graph API');

    return {
      data: buffer.toString('base64'),
      mimetype,
    };
  } catch (error) {
    logger.error({ error, mediaId }, 'Error extracting media data from Cloud API');
    return null;
  }
}
```

## Step 2: Register in `routes/webhook.ts`

Insert into the `switch (message.type)` block in `packages/whatsapp-cloud/src/routes/webhook.ts`:

```typescript
case 'my_media': {
  const mediaId = getMediaId(message);
  if (!mediaId) {
    logger.warn({ messageId, senderPhone }, 'Media message without media ID');
    break;
  }

  const mediaData = await extractMyMediaData(mediaId);
  if (!mediaData) {
    await sendReaction(senderPhone, messageId, '❌');
    break;
  }

  const caption = message.my_media?.caption || 'Default prompt';
  await handleTextMessage(senderPhone, messageId, caption, senderName, mediaData);
  break;
}
```

## Cloud API Gotchas & Invariants
- **Phone vs JID**: Cloud API operates with raw phone numbers (e.g. `"16505551234"`). JID conversion uses `phoneToJid()` inside `handleTextMessage()`.
- **Typing Indicator**: `graphApi.sendTypingIndicator(messageId)` marks the message as read and activates typing. It auto-dismisses after 25s or when a reply is sent (`paused` presence is a no-op).
- **Error Reactions**: Use `graphApi.sendReaction(to, messageId, '❌')`.
- **24-Hour Window**: Meta restricts replies to within 24 hours of the user's last inbound message (`cloud_last_inbound_at`).
- **Webhook Verification**: Incoming webhook requests use `x-hub-signature-256` HMAC-SHA256, skipping `X-API-Key` auth.
