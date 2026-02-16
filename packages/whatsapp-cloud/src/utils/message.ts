/**
 * Webhook payload extraction utilities for the WhatsApp Cloud API.
 *
 * The Cloud API webhook payload structure:
 * {
 *   "object": "whatsapp_business_account",
 *   "entry": [{
 *     "id": "WABA_ID",
 *     "changes": [{
 *       "value": {
 *         "messaging_product": "whatsapp",
 *         "metadata": { "phone_number_id": "...", "display_phone_number": "..." },
 *         "contacts": [{ "profile": { "name": "Jane" }, "wa_id": "16505551234" }],
 *         "messages": [{ "from": "...", "id": "wamid.HBg...", "timestamp": "...", "type": "text", ... }],
 *         "statuses": [{ "id": "...", "status": "...", "timestamp": "..." }]
 *       }
 *     }]
 *   }]
 * }
 */

// ---------------------------------------------------------------------------
// Type definitions for the webhook payload
// ---------------------------------------------------------------------------

export interface WebhookContact {
  profile?: { name?: string };
  wa_id: string;
}

export interface WebhookMetadata {
  phone_number_id: string;
  display_phone_number: string;
}

export interface WebhookTextMessage {
  body: string;
}

export interface WebhookMediaMessage {
  id: string;
  mime_type?: string;
  sha256?: string;
  caption?: string;
}

export interface WebhookDocumentMessage extends WebhookMediaMessage {
  filename?: string;
}

export interface WebhookLocationMessage {
  latitude: number;
  longitude: number;
  name?: string;
  address?: string;
}

export interface WebhookContextInfo {
  from?: string;
  id?: string;
  mentioned_jids?: string[];
}

export interface WebhookReactionMessage {
  message_id: string;
  emoji: string;
}

export interface WebhookMessage {
  from: string;
  id: string;
  timestamp: string;
  type: string;
  text?: WebhookTextMessage;
  image?: WebhookMediaMessage;
  audio?: WebhookMediaMessage;
  video?: WebhookMediaMessage;
  document?: WebhookDocumentMessage;
  location?: WebhookLocationMessage;
  reaction?: WebhookReactionMessage;
  context?: WebhookContextInfo;
}

export interface WebhookStatus {
  id: string;
  status: string;
  timestamp: string;
  recipient_id: string;
}

export interface WebhookChangeValue {
  messaging_product: string;
  metadata: WebhookMetadata;
  contacts?: WebhookContact[];
  messages?: WebhookMessage[];
  statuses?: WebhookStatus[];
}

export interface WebhookChange {
  value: WebhookChangeValue;
  field: string;
}

export interface WebhookEntry {
  id: string;
  changes: WebhookChange[];
}

export interface WebhookBody {
  object: string;
  entry: WebhookEntry[];
}

// ---------------------------------------------------------------------------
// Extracted message bundle (message + its associated contact + metadata)
// ---------------------------------------------------------------------------

export interface ExtractedMessage {
  message: WebhookMessage;
  contact: WebhookContact | undefined;
  metadata: WebhookMetadata;
}

// ---------------------------------------------------------------------------
// Utility functions
// ---------------------------------------------------------------------------

/**
 * Extract all messages from a webhook payload.
 * Iterates entry[].changes[].value and pairs each message with its contact and metadata.
 */
export function extractMessages(webhookBody: WebhookBody): ExtractedMessage[] {
  const results: ExtractedMessage[] = [];

  for (const entry of webhookBody.entry) {
    for (const change of entry.changes) {
      const { value } = change;
      const messages = value.messages || [];
      const contacts = value.contacts || [];
      const { metadata } = value;

      for (const message of messages) {
        // Match contact by wa_id === message.from
        const contact = contacts.find((c) => c.wa_id === message.from);
        results.push({ message, contact, metadata });
      }
    }
  }

  return results;
}

/**
 * Get the sender phone number from a webhook message.
 */
export function getSenderPhone(message: WebhookMessage): string {
  return message.from;
}

/**
 * Get the sender display name from a webhook contact.
 * Falls back to wa_id if no profile name is available.
 */
export function getSenderName(contact: WebhookContact | undefined): string {
  return contact?.profile?.name || contact?.wa_id || 'Unknown';
}

/**
 * Extract the text body from a webhook message based on its type.
 * - text -> text.body
 * - image -> image.caption
 * - video -> video.caption
 * - document -> document.caption
 * - location -> formatted location string
 */
export function getMessageText(message: WebhookMessage): string | undefined {
  switch (message.type) {
    case 'text':
      return message.text?.body;
    case 'image':
      return message.image?.caption;
    case 'video':
      return message.video?.caption;
    case 'document':
      return message.document?.caption;
    case 'location':
      if (message.location) {
        const parts = [
          message.location.name,
          message.location.address,
          `(${message.location.latitude}, ${message.location.longitude})`,
        ].filter(Boolean);
        return parts.join(' - ');
      }
      return undefined;
    default:
      return undefined;
  }
}

/**
 * Extract the media ID from a webhook message based on its type.
 */
export function getMediaId(message: WebhookMessage): string | undefined {
  switch (message.type) {
    case 'image':
      return message.image?.id;
    case 'audio':
      return message.audio?.id;
    case 'video':
      return message.video?.id;
    case 'document':
      return message.document?.id;
    default:
      return undefined;
  }
}

/**
 * Get the filename from a document message.
 */
export function getDocumentFilename(message: WebhookMessage): string | undefined {
  if (message.type === 'document') {
    return message.document?.filename;
  }
  return undefined;
}

/**
 * Check if the webhook payload is a status update (delivery receipts, read receipts)
 * rather than an incoming message.
 */
export function isStatusUpdate(webhookBody: WebhookBody): boolean {
  for (const entry of webhookBody.entry) {
    for (const change of entry.changes) {
      if (change.value.statuses && change.value.statuses.length > 0) {
        return true;
      }
    }
  }
  return false;
}
