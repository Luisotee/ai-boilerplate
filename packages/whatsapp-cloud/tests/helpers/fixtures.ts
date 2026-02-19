/**
 * Webhook body factory and mock Graph API service for whatsapp-cloud tests.
 *
 * These helpers produce structurally correct Cloud API webhook payloads
 * and a mock graph-api service object whose methods are vi.fn() stubs.
 */

import { vi } from 'vitest';

// ---------------------------------------------------------------------------
// Webhook body factory
// ---------------------------------------------------------------------------

/**
 * Build a valid Cloud API webhook POST payload containing a single text
 * message from the given phone number.
 *
 * The returned object satisfies both the Zod `WebhookBodySchema` and the
 * `WebhookBody` TypeScript interface in `utils/message.ts`.
 *
 * @param phone - Sender phone number (e.g. '16505551234').
 * @param text  - The text message body.
 * @returns A webhook payload object ready to be POSTed to `/webhook`.
 */
export function makeWebhookBody(phone: string, text: string) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const messageId = `wamid.HBg${Date.now()}`;

  return {
    object: 'whatsapp_business_account',
    entry: [
      {
        id: 'WABA_ID',
        changes: [
          {
            value: {
              messaging_product: 'whatsapp',
              metadata: {
                phone_number_id: 'PHONE_NUMBER_ID',
                display_phone_number: '+15550001234',
              },
              contacts: [
                {
                  profile: { name: 'Test User' },
                  wa_id: phone,
                },
              ],
              messages: [
                {
                  from: phone,
                  id: messageId,
                  timestamp,
                  type: 'text',
                  text: { body: text },
                },
              ],
            },
            field: 'messages',
          },
        ],
      },
    ],
  };
}

/**
 * Build a webhook payload for an image message.
 *
 * @param phone   - Sender phone number.
 * @param mediaId - The Graph API media ID for the image.
 * @param caption - Optional image caption.
 */
export function makeImageWebhookBody(phone: string, mediaId: string, caption?: string) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const messageId = `wamid.HBg${Date.now()}`;

  return {
    object: 'whatsapp_business_account',
    entry: [
      {
        id: 'WABA_ID',
        changes: [
          {
            value: {
              messaging_product: 'whatsapp',
              metadata: {
                phone_number_id: 'PHONE_NUMBER_ID',
                display_phone_number: '+15550001234',
              },
              contacts: [
                {
                  profile: { name: 'Test User' },
                  wa_id: phone,
                },
              ],
              messages: [
                {
                  from: phone,
                  id: messageId,
                  timestamp,
                  type: 'image',
                  image: {
                    id: mediaId,
                    mime_type: 'image/jpeg',
                    ...(caption ? { caption } : {}),
                  },
                },
              ],
            },
            field: 'messages',
          },
        ],
      },
    ],
  };
}

/**
 * Build a webhook payload for a document message.
 *
 * @param phone    - Sender phone number.
 * @param mediaId  - The Graph API media ID for the document.
 * @param filename - Document filename.
 * @param caption  - Optional document caption.
 */
export function makeDocumentWebhookBody(
  phone: string,
  mediaId: string,
  filename: string,
  caption?: string
) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const messageId = `wamid.HBg${Date.now()}`;

  return {
    object: 'whatsapp_business_account',
    entry: [
      {
        id: 'WABA_ID',
        changes: [
          {
            value: {
              messaging_product: 'whatsapp',
              metadata: {
                phone_number_id: 'PHONE_NUMBER_ID',
                display_phone_number: '+15550001234',
              },
              contacts: [
                {
                  profile: { name: 'Test User' },
                  wa_id: phone,
                },
              ],
              messages: [
                {
                  from: phone,
                  id: messageId,
                  timestamp,
                  type: 'document',
                  document: {
                    id: mediaId,
                    mime_type: 'application/pdf',
                    filename,
                    ...(caption ? { caption } : {}),
                  },
                },
              ],
            },
            field: 'messages',
          },
        ],
      },
    ],
  };
}

/**
 * Build a minimal status-update webhook payload (delivery/read receipts).
 * These should NOT trigger message processing.
 *
 * @param phone  - Recipient phone number.
 * @param status - Status value (e.g. 'delivered', 'read', 'sent').
 */
export function makeStatusWebhookBody(phone: string, status: string) {
  return {
    object: 'whatsapp_business_account',
    entry: [
      {
        id: 'WABA_ID',
        changes: [
          {
            value: {
              messaging_product: 'whatsapp',
              metadata: {
                phone_number_id: 'PHONE_NUMBER_ID',
                display_phone_number: '+15550001234',
              },
              statuses: [
                {
                  id: `wamid.status_${Date.now()}`,
                  status,
                  timestamp: String(Math.floor(Date.now() / 1000)),
                  recipient_id: phone,
                },
              ],
            },
            field: 'messages',
          },
        ],
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Mock Graph API service
// ---------------------------------------------------------------------------

/**
 * Build a mock of the graph-api service module (`services/graph-api.ts`).
 *
 * Every exported function is replaced with a `vi.fn()` that resolves to a
 * sensible default value. Tests can override return values per-test via
 * `.mockResolvedValueOnce()` etc.
 *
 * Covers all public functions:
 *  - sendText, sendReaction, sendLocation, sendContact
 *  - sendImage, sendDocument, sendAudio, sendVideo
 *  - markAsRead, sendTypingIndicator
 *  - uploadMedia, downloadMedia
 */
export function makeMockGraphApi() {
  return {
    // Sending messages
    sendText: vi.fn().mockResolvedValue('wamid.sent_text_123'),
    sendReaction: vi.fn().mockResolvedValue(undefined),
    sendLocation: vi.fn().mockResolvedValue('wamid.sent_location_123'),
    sendContact: vi.fn().mockResolvedValue('wamid.sent_contact_123'),

    // Media messages
    sendImage: vi.fn().mockResolvedValue('wamid.sent_image_123'),
    sendDocument: vi.fn().mockResolvedValue('wamid.sent_document_123'),
    sendAudio: vi.fn().mockResolvedValue('wamid.sent_audio_123'),
    sendVideo: vi.fn().mockResolvedValue('wamid.sent_video_123'),

    // Read receipts and typing
    markAsRead: vi.fn().mockResolvedValue(undefined),
    sendTypingIndicator: vi.fn().mockResolvedValue(undefined),

    // Media upload/download
    uploadMedia: vi.fn().mockResolvedValue('media_upload_id_123'),
    downloadMedia: vi.fn().mockResolvedValue({
      buffer: Buffer.from('fake-media-content'),
      mimetype: 'application/octet-stream',
    }),
  };
}
