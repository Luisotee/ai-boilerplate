/**
 * Meta Graph API HTTP client for the WhatsApp Cloud API.
 *
 * All outbound WhatsApp interactions (send messages, upload/download media,
 * mark-as-read) go through this module.
 *
 * Base URL pattern: https://graph.facebook.com/{version}/{phone_number_id}/messages
 * Auth: Bearer token via META_ACCESS_TOKEN
 */

import { config } from '../config.js';
import { logger } from '../logger.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface GraphApiResponse {
  messaging_product: string;
  contacts?: { input: string; wa_id: string }[];
  messages?: { id: string }[];
}

interface MediaUploadResponse {
  id: string;
}

interface MediaUrlResponse {
  url: string;
  mime_type: string;
  sha256: string;
  file_size: number;
  id: string;
  messaging_product: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const { phoneNumberId, accessToken, graphApiVersion, graphApiBaseUrl } = config.meta;

/**
 * Build the messages endpoint URL.
 */
function getMessagesUrl(): string {
  return `${graphApiBaseUrl}/${graphApiVersion}/${phoneNumberId}/messages`;
}

/**
 * Build authorization and content-type headers.
 */
function getHeaders(contentType = 'application/json'): Record<string, string> {
  return {
    Authorization: `Bearer ${accessToken}`,
    'Content-Type': contentType,
  };
}

/**
 * Send a message via the Graph API.
 * Shared POST logic with structured error handling.
 *
 * @returns The message ID returned by the API.
 */
async function sendMessage(to: string, messageBody: Record<string, unknown>): Promise<string> {
  const url = getMessagesUrl();
  const body = {
    messaging_product: 'whatsapp',
    to,
    ...messageBody,
  };

  logger.debug({ to, type: messageBody.type }, 'Sending message via Graph API');

  const response = await fetch(url, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    logger.error({ status: response.status, body: errorBody, to }, 'Graph API message send failed');
    throw new Error(`Graph API error ${response.status}: ${errorBody}`);
  }

  const data = (await response.json()) as GraphApiResponse;
  const messageId = data.messages?.[0]?.id || '';

  logger.debug({ messageId, to }, 'Message sent successfully');
  return messageId;
}

// ---------------------------------------------------------------------------
// Public API — Sending messages
// ---------------------------------------------------------------------------

/**
 * Send a text message.
 * Optionally quote/reply to a specific message via the context parameter.
 */
export async function sendText(
  to: string,
  text: string,
  context?: { message_id: string }
): Promise<string> {
  const body: Record<string, unknown> = {
    type: 'text',
    text: { body: text },
  };
  if (context) {
    body.context = context;
  }
  return sendMessage(to, body);
}

/**
 * Send a reaction emoji to a specific message.
 */
export async function sendReaction(to: string, messageId: string, emoji: string): Promise<void> {
  await sendMessage(to, {
    type: 'reaction',
    reaction: {
      message_id: messageId,
      emoji,
    },
  });
}

/**
 * Send a location message.
 */
export async function sendLocation(
  to: string,
  latitude: number,
  longitude: number,
  name?: string,
  address?: string
): Promise<string> {
  return sendMessage(to, {
    type: 'location',
    location: {
      latitude,
      longitude,
      ...(name && { name }),
      ...(address && { address }),
    },
  });
}

/**
 * Send a contact card.
 */
export async function sendContact(
  to: string,
  contactName: string,
  contactPhone: string,
  contactEmail?: string,
  contactOrg?: string
): Promise<string> {
  const contact: Record<string, unknown> = {
    name: { formatted_name: contactName },
    phones: [{ phone: contactPhone, type: 'CELL' }],
  };

  if (contactEmail) {
    contact.emails = [{ email: contactEmail, type: 'WORK' }];
  }
  if (contactOrg) {
    contact.org = { company: contactOrg };
  }

  return sendMessage(to, {
    type: 'contacts',
    contacts: [contact],
  });
}

// ---------------------------------------------------------------------------
// Public API — Media messages
// ---------------------------------------------------------------------------

/**
 * Send an image message. Uploads the buffer first, then sends with media_id.
 */
export async function sendImage(
  to: string,
  buffer: Buffer,
  mimetype: string,
  caption?: string
): Promise<string> {
  const mediaId = await uploadMedia(buffer, mimetype, 'image');
  const image: Record<string, unknown> = { id: mediaId };
  if (caption) {
    image.caption = caption;
  }
  return sendMessage(to, { type: 'image', image });
}

/**
 * Send a document message. Uploads the buffer first, then sends with media_id.
 */
export async function sendDocument(
  to: string,
  buffer: Buffer,
  mimetype: string,
  filename: string,
  caption?: string
): Promise<string> {
  const mediaId = await uploadMedia(buffer, mimetype, filename);
  const document: Record<string, unknown> = { id: mediaId, filename };
  if (caption) {
    document.caption = caption;
  }
  return sendMessage(to, { type: 'document', document });
}

/**
 * Send an audio message. Uploads the buffer first, then sends with media_id.
 */
export async function sendAudio(to: string, buffer: Buffer, mimetype: string): Promise<string> {
  const mediaId = await uploadMedia(buffer, mimetype, 'audio');
  return sendMessage(to, { type: 'audio', audio: { id: mediaId } });
}

/**
 * Send a video message. Uploads the buffer first, then sends with media_id.
 */
export async function sendVideo(
  to: string,
  buffer: Buffer,
  mimetype: string,
  caption?: string
): Promise<string> {
  const mediaId = await uploadMedia(buffer, mimetype, 'video');
  const video: Record<string, unknown> = { id: mediaId };
  if (caption) {
    video.caption = caption;
  }
  return sendMessage(to, { type: 'video', video });
}

// ---------------------------------------------------------------------------
// Public API — Read receipts
// ---------------------------------------------------------------------------

/**
 * Mark a message as read.
 */
export async function markAsRead(messageId: string): Promise<void> {
  const url = getMessagesUrl();
  const body = {
    messaging_product: 'whatsapp',
    status: 'read',
    message_id: messageId,
  };

  logger.debug({ messageId }, 'Marking message as read');

  const response = await fetch(url, {
    method: 'POST',
    headers: getHeaders(),
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    logger.error(
      { status: response.status, body: errorBody, messageId },
      'Failed to mark message as read'
    );
    throw new Error(`Graph API error ${response.status}: ${errorBody}`);
  }

  logger.debug({ messageId }, 'Message marked as read');
}

// ---------------------------------------------------------------------------
// Public API — Media upload / download
// ---------------------------------------------------------------------------

/**
 * Upload a media file to the Graph API.
 *
 * POST ${baseUrl}/${version}/${phoneNumberId}/media
 * Body: multipart/form-data with messaging_product, type, and file fields.
 *
 * @returns The media ID for use in message sends.
 */
export async function uploadMedia(
  buffer: Buffer,
  mimetype: string,
  filename?: string
): Promise<string> {
  const url = `${graphApiBaseUrl}/${graphApiVersion}/${phoneNumberId}/media`;

  const formData = new FormData();
  formData.append('messaging_product', 'whatsapp');
  formData.append('type', mimetype);
  formData.append(
    'file',
    new Blob([new Uint8Array(buffer)], { type: mimetype }),
    filename || 'file'
  );

  logger.debug({ mimetype, size: buffer.length }, 'Uploading media to Graph API');

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      // Content-Type is set automatically for FormData
    },
    body: formData,
  });

  if (!response.ok) {
    const errorBody = await response.text();
    logger.error({ status: response.status, body: errorBody }, 'Media upload failed');
    throw new Error(`Graph API media upload error ${response.status}: ${errorBody}`);
  }

  const data = (await response.json()) as MediaUploadResponse;
  logger.debug({ mediaId: data.id }, 'Media uploaded successfully');
  return data.id;
}

/**
 * Download a media file from the Graph API.
 *
 * Two-step process:
 * 1. GET ${baseUrl}/${version}/${mediaId} → returns { url: "..." }
 * 2. GET the URL with auth headers → returns binary data
 *
 * @returns The file buffer and its mimetype.
 */
export async function downloadMedia(
  mediaId: string
): Promise<{ buffer: Buffer; mimetype: string }> {
  // Step 1: Get the media URL
  const metadataUrl = `${graphApiBaseUrl}/${graphApiVersion}/${mediaId}`;

  logger.debug({ mediaId }, 'Fetching media URL from Graph API');

  const metadataResponse = await fetch(metadataUrl, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  if (!metadataResponse.ok) {
    const errorBody = await metadataResponse.text();
    logger.error(
      { status: metadataResponse.status, body: errorBody, mediaId },
      'Failed to fetch media metadata'
    );
    throw new Error(`Graph API media metadata error ${metadataResponse.status}: ${errorBody}`);
  }

  const metadata = (await metadataResponse.json()) as MediaUrlResponse;

  // Step 2: Download the actual file
  logger.debug({ mediaId, url: metadata.url }, 'Downloading media binary');

  const fileResponse = await fetch(metadata.url, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${accessToken}`,
    },
  });

  if (!fileResponse.ok) {
    const errorBody = await fileResponse.text();
    logger.error(
      { status: fileResponse.status, body: errorBody, mediaId },
      'Failed to download media file'
    );
    throw new Error(`Graph API media download error ${fileResponse.status}: ${errorBody}`);
  }

  const arrayBuffer = await fileResponse.arrayBuffer();
  const buffer = Buffer.from(arrayBuffer);
  const mimetype =
    metadata.mime_type || fileResponse.headers.get('content-type') || 'application/octet-stream';

  logger.debug({ mediaId, mimetype, size: buffer.length }, 'Media downloaded successfully');

  return { buffer, mimetype };
}
