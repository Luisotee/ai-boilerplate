import { describe, it, expect } from 'vitest';
import {
  extractMessages,
  getSenderPhone,
  getSenderName,
  getMessageText,
  getMediaId,
  getDocumentFilename,
  isStatusUpdate,
} from '../../src/utils/message.js';
import type {
  WebhookBody,
  WebhookMessage,
  WebhookContact,
  WebhookMetadata,
} from '../../src/utils/message.js';

// ---------------------------------------------------------------------------
// Helper: build a minimal webhook body
// ---------------------------------------------------------------------------

function makeWebhookBody(
  overrides: {
    messages?: WebhookMessage[];
    contacts?: WebhookContact[];
    statuses?: Array<{ id: string; status: string; timestamp: string; recipient_id: string }>;
    metadata?: WebhookMetadata;
  } = {}
): WebhookBody {
  const metadata = overrides.metadata ?? {
    phone_number_id: 'PHONE_NUMBER_ID',
    display_phone_number: '+15550001234',
  };

  return {
    object: 'whatsapp_business_account',
    entry: [
      {
        id: 'WABA_ID',
        changes: [
          {
            value: {
              messaging_product: 'whatsapp',
              metadata,
              contacts: overrides.contacts,
              messages: overrides.messages,
              statuses: overrides.statuses,
            },
            field: 'messages',
          },
        ],
      },
    ],
  };
}

function makeTextMessage(overrides: Partial<WebhookMessage> = {}): WebhookMessage {
  return {
    from: '16505551234',
    id: 'wamid.HBgNMTY1MDU1NTEyMzQ',
    timestamp: '1700000000',
    type: 'text',
    text: { body: 'Hello, world!' },
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// extractMessages
// ---------------------------------------------------------------------------

describe('extractMessages', () => {
  it('should extract a single message with its matched contact', () => {
    const message = makeTextMessage();
    const contact: WebhookContact = {
      profile: { name: 'John Doe' },
      wa_id: '16505551234',
    };
    const body = makeWebhookBody({ messages: [message], contacts: [contact] });

    const results = extractMessages(body);

    expect(results).toHaveLength(1);
    expect(results[0].message).toBe(message);
    expect(results[0].contact).toBe(contact);
    expect(results[0].metadata.phone_number_id).toBe('PHONE_NUMBER_ID');
  });

  it('should extract multiple messages', () => {
    const msg1 = makeTextMessage({ from: '111', id: 'id1' });
    const msg2 = makeTextMessage({ from: '222', id: 'id2' });
    const contacts: WebhookContact[] = [
      { profile: { name: 'Alice' }, wa_id: '111' },
      { profile: { name: 'Bob' }, wa_id: '222' },
    ];
    const body = makeWebhookBody({ messages: [msg1, msg2], contacts });

    const results = extractMessages(body);

    expect(results).toHaveLength(2);
    expect(results[0].contact?.profile?.name).toBe('Alice');
    expect(results[1].contact?.profile?.name).toBe('Bob');
  });

  it('should return empty array when there are no messages', () => {
    const body = makeWebhookBody({ messages: undefined });
    const results = extractMessages(body);
    expect(results).toHaveLength(0);
  });

  it('should return empty array when messages array is empty', () => {
    const body = makeWebhookBody({ messages: [] });
    const results = extractMessages(body);
    expect(results).toHaveLength(0);
  });

  it('should set contact to undefined when no matching contact found', () => {
    const message = makeTextMessage({ from: '999' });
    const contacts: WebhookContact[] = [{ profile: { name: 'Alice' }, wa_id: '111' }];
    const body = makeWebhookBody({ messages: [message], contacts });

    const results = extractMessages(body);

    expect(results).toHaveLength(1);
    expect(results[0].contact).toBeUndefined();
  });

  it('should set contact to undefined when contacts array is absent', () => {
    const message = makeTextMessage();
    const body = makeWebhookBody({ messages: [message], contacts: undefined });

    const results = extractMessages(body);

    expect(results).toHaveLength(1);
    expect(results[0].contact).toBeUndefined();
  });

  it('should handle multiple entries', () => {
    const msg1 = makeTextMessage({ id: 'entry1_msg' });
    const msg2 = makeTextMessage({ id: 'entry2_msg', from: '222' });

    const body: WebhookBody = {
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'ENTRY1',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: { phone_number_id: 'PN1', display_phone_number: '+1' },
                messages: [msg1],
                contacts: [{ wa_id: '16505551234', profile: { name: 'User1' } }],
              },
              field: 'messages',
            },
          ],
        },
        {
          id: 'ENTRY2',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: { phone_number_id: 'PN2', display_phone_number: '+2' },
                messages: [msg2],
                contacts: [{ wa_id: '222', profile: { name: 'User2' } }],
              },
              field: 'messages',
            },
          ],
        },
      ],
    };

    const results = extractMessages(body);

    expect(results).toHaveLength(2);
    expect(results[0].metadata.phone_number_id).toBe('PN1');
    expect(results[1].metadata.phone_number_id).toBe('PN2');
  });

  it('should handle multiple changes within one entry', () => {
    const msg1 = makeTextMessage({ id: 'change1_msg' });
    const msg2 = makeTextMessage({ id: 'change2_msg', from: '333' });

    const body: WebhookBody = {
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'ENTRY1',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: { phone_number_id: 'PN1', display_phone_number: '+1' },
                messages: [msg1],
              },
              field: 'messages',
            },
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: { phone_number_id: 'PN2', display_phone_number: '+2' },
                messages: [msg2],
                contacts: [{ wa_id: '333', profile: { name: 'User3' } }],
              },
              field: 'messages',
            },
          ],
        },
      ],
    };

    const results = extractMessages(body);

    expect(results).toHaveLength(2);
    expect(results[0].contact).toBeUndefined();
    expect(results[1].contact?.wa_id).toBe('333');
  });
});

// ---------------------------------------------------------------------------
// getSenderPhone
// ---------------------------------------------------------------------------

describe('getSenderPhone', () => {
  it('should return the from field of the message', () => {
    const message = makeTextMessage({ from: '16505551234' });
    expect(getSenderPhone(message)).toBe('16505551234');
  });

  it('should return the from field for a different phone', () => {
    const message = makeTextMessage({ from: '5511999999999' });
    expect(getSenderPhone(message)).toBe('5511999999999');
  });
});

// ---------------------------------------------------------------------------
// getSenderName
// ---------------------------------------------------------------------------

describe('getSenderName', () => {
  it('should return profile name when available', () => {
    const contact: WebhookContact = { profile: { name: 'Jane Doe' }, wa_id: '16505551234' };
    expect(getSenderName(contact)).toBe('Jane Doe');
  });

  it('should fall back to wa_id when profile name is missing', () => {
    const contact: WebhookContact = { profile: {}, wa_id: '16505551234' };
    expect(getSenderName(contact)).toBe('16505551234');
  });

  it('should fall back to wa_id when profile is missing', () => {
    const contact: WebhookContact = { wa_id: '16505551234' };
    expect(getSenderName(contact)).toBe('16505551234');
  });

  it('should return Unknown when contact is undefined', () => {
    expect(getSenderName(undefined)).toBe('Unknown');
  });

  it('should fall back to wa_id when name is undefined', () => {
    const contact: WebhookContact = { profile: { name: undefined }, wa_id: '5511999999999' };
    expect(getSenderName(contact)).toBe('5511999999999');
  });

  it('should return empty string name if it is empty (falsy)', () => {
    // Empty string is falsy, so it should fall back to wa_id
    const contact: WebhookContact = { profile: { name: '' }, wa_id: '111' };
    expect(getSenderName(contact)).toBe('111');
  });
});

// ---------------------------------------------------------------------------
// getMessageText
// ---------------------------------------------------------------------------

describe('getMessageText', () => {
  it('should return text body for text messages', () => {
    const message = makeTextMessage();
    expect(getMessageText(message)).toBe('Hello, world!');
  });

  it('should return image caption for image messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'image',
      image: { id: 'media_id', caption: 'A beautiful sunset' },
    };
    expect(getMessageText(message)).toBe('A beautiful sunset');
  });

  it('should return undefined for image without caption', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'image',
      image: { id: 'media_id' },
    };
    expect(getMessageText(message)).toBeUndefined();
  });

  it('should return video caption for video messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'video',
      video: { id: 'media_id', caption: 'Check this out' },
    };
    expect(getMessageText(message)).toBe('Check this out');
  });

  it('should return document caption for document messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'document',
      document: { id: 'media_id', caption: 'Important doc', filename: 'report.pdf' },
    };
    expect(getMessageText(message)).toBe('Important doc');
  });

  it('should return formatted location string with all parts', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'location',
      location: {
        latitude: -23.5505,
        longitude: -46.6333,
        name: 'Sao Paulo',
        address: 'SP, Brazil',
      },
    };
    const text = getMessageText(message);
    expect(text).toBe('Sao Paulo - SP, Brazil - (-23.5505, -46.6333)');
  });

  it('should return location string with coordinates only when name and address are absent', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'location',
      location: { latitude: 40.7128, longitude: -74.006 },
    };
    const text = getMessageText(message);
    expect(text).toBe('(40.7128, -74.006)');
  });

  it('should return location string with name only (no address)', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'location',
      location: { latitude: 40.7128, longitude: -74.006, name: 'NYC' },
    };
    const text = getMessageText(message);
    expect(text).toBe('NYC - (40.7128, -74.006)');
  });

  it('should return undefined for location type when location object is absent', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'location',
    };
    expect(getMessageText(message)).toBeUndefined();
  });

  it('should return undefined for audio messages (no text)', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'audio',
      audio: { id: 'audio_id' },
    };
    expect(getMessageText(message)).toBeUndefined();
  });

  it('should return undefined for reaction messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'reaction',
      reaction: { message_id: 'orig_id', emoji: '👍' },
    };
    expect(getMessageText(message)).toBeUndefined();
  });

  it('should return undefined for unknown message types', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'sticker',
    };
    expect(getMessageText(message)).toBeUndefined();
  });

  it('should return undefined for text message with missing text object', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'text',
    };
    expect(getMessageText(message)).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// getMediaId
// ---------------------------------------------------------------------------

describe('getMediaId', () => {
  it('should return media ID for image message', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'image',
      image: { id: 'img_media_id' },
    };
    expect(getMediaId(message)).toBe('img_media_id');
  });

  it('should return media ID for audio message', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'audio',
      audio: { id: 'audio_media_id' },
    };
    expect(getMediaId(message)).toBe('audio_media_id');
  });

  it('should return media ID for video message', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'video',
      video: { id: 'video_media_id' },
    };
    expect(getMediaId(message)).toBe('video_media_id');
  });

  it('should return media ID for document message', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'document',
      document: { id: 'doc_media_id', filename: 'file.pdf' },
    };
    expect(getMediaId(message)).toBe('doc_media_id');
  });

  it('should return undefined for text messages', () => {
    const message = makeTextMessage();
    expect(getMediaId(message)).toBeUndefined();
  });

  it('should return undefined for location messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'location',
      location: { latitude: 0, longitude: 0 },
    };
    expect(getMediaId(message)).toBeUndefined();
  });

  it('should return undefined for reaction messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'reaction',
      reaction: { message_id: 'mid', emoji: '👍' },
    };
    expect(getMediaId(message)).toBeUndefined();
  });

  it('should return undefined for unknown message types', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'sticker',
    };
    expect(getMediaId(message)).toBeUndefined();
  });

  it('should return undefined when image object is missing', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'image',
    };
    expect(getMediaId(message)).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// getDocumentFilename
// ---------------------------------------------------------------------------

describe('getDocumentFilename', () => {
  it('should return filename for document message', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'document',
      document: { id: 'doc_id', filename: 'report.pdf' },
    };
    expect(getDocumentFilename(message)).toBe('report.pdf');
  });

  it('should return undefined when document has no filename', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'document',
      document: { id: 'doc_id' },
    };
    expect(getDocumentFilename(message)).toBeUndefined();
  });

  it('should return undefined for non-document types', () => {
    const message = makeTextMessage();
    expect(getDocumentFilename(message)).toBeUndefined();
  });

  it('should return undefined for image messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'image',
      image: { id: 'img_id' },
    };
    expect(getDocumentFilename(message)).toBeUndefined();
  });

  it('should return undefined for audio messages', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'audio',
      audio: { id: 'audio_id' },
    };
    expect(getDocumentFilename(message)).toBeUndefined();
  });

  it('should return undefined when document type but document object is missing', () => {
    const message: WebhookMessage = {
      from: '111',
      id: 'id1',
      timestamp: '123',
      type: 'document',
    };
    expect(getDocumentFilename(message)).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// isStatusUpdate
// ---------------------------------------------------------------------------

describe('isStatusUpdate', () => {
  it('should return true when statuses array has entries', () => {
    const body = makeWebhookBody({
      statuses: [
        { id: 'wamid.xyz', status: 'delivered', timestamp: '1700000000', recipient_id: '111' },
      ],
    });
    expect(isStatusUpdate(body)).toBe(true);
  });

  it('should return false when statuses array is empty', () => {
    const body = makeWebhookBody({ statuses: [] });
    expect(isStatusUpdate(body)).toBe(false);
  });

  it('should return false when statuses is absent', () => {
    const body = makeWebhookBody({ messages: [makeTextMessage()] });
    expect(isStatusUpdate(body)).toBe(false);
  });

  it('should return true even if messages are also present', () => {
    const body = makeWebhookBody({
      messages: [makeTextMessage()],
      statuses: [
        { id: 'wamid.xyz', status: 'read', timestamp: '1700000000', recipient_id: '111' },
      ],
    });
    expect(isStatusUpdate(body)).toBe(true);
  });

  it('should return true if any change in any entry has statuses', () => {
    const body: WebhookBody = {
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'E1',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: { phone_number_id: 'PN', display_phone_number: '+1' },
              },
              field: 'messages',
            },
          ],
        },
        {
          id: 'E2',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: { phone_number_id: 'PN', display_phone_number: '+1' },
                statuses: [
                  { id: 's1', status: 'sent', timestamp: '123', recipient_id: '111' },
                ],
              },
              field: 'messages',
            },
          ],
        },
      ],
    };
    expect(isStatusUpdate(body)).toBe(true);
  });

  it('should return false for webhook body with only messages (no statuses key)', () => {
    const body = makeWebhookBody({ messages: [makeTextMessage()] });
    expect(isStatusUpdate(body)).toBe(false);
  });
});
