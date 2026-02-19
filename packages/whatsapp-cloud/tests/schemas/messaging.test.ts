import { describe, it, expect } from 'vitest';
import {
  SendTextSchema,
  SendReactionSchema,
  TypingIndicatorSchema,
  ReadMessagesSchema,
  EditMessageSchema,
  DeleteMessageSchema,
  SendTextResponseSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
  HealthResponseSchema,
} from '../../src/schemas/messaging.js';

// ---------------------------------------------------------------------------
// SendTextSchema
// ---------------------------------------------------------------------------

describe('SendTextSchema', () => {
  it('should accept valid send text request', () => {
    const result = SendTextSchema.safeParse({
      phoneNumber: '5511999999999',
      text: 'Hello!',
    });
    expect(result.success).toBe(true);
  });

  it('should accept request with optional quoted_message_id', () => {
    const result = SendTextSchema.safeParse({
      phoneNumber: '5511999999999',
      text: 'Reply text',
      quoted_message_id: 'wamid.HBgNMTY1MDU1NTEyMzQ',
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.quoted_message_id).toBe('wamid.HBgNMTY1MDU1NTEyMzQ');
    }
  });

  it('should accept request without quoted_message_id', () => {
    const result = SendTextSchema.safeParse({
      phoneNumber: '16505551234',
      text: 'Hello',
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.quoted_message_id).toBeUndefined();
    }
  });

  it('should reject missing phoneNumber', () => {
    const result = SendTextSchema.safeParse({ text: 'Hello' });
    expect(result.success).toBe(false);
  });

  it('should reject missing text', () => {
    const result = SendTextSchema.safeParse({ phoneNumber: '111' });
    expect(result.success).toBe(false);
  });

  it('should reject empty text', () => {
    const result = SendTextSchema.safeParse({ phoneNumber: '111', text: '' });
    expect(result.success).toBe(false);
  });

  it('should reject empty object', () => {
    const result = SendTextSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it('should accept long text', () => {
    const result = SendTextSchema.safeParse({
      phoneNumber: '111',
      text: 'x'.repeat(10000),
    });
    expect(result.success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// SendReactionSchema
// ---------------------------------------------------------------------------

describe('SendReactionSchema', () => {
  it('should accept valid reaction request', () => {
    const result = SendReactionSchema.safeParse({
      phoneNumber: '5511999999999',
      message_id: 'wamid.abc123',
      emoji: '👍',
    });
    expect(result.success).toBe(true);
  });

  it('should reject missing phoneNumber', () => {
    const result = SendReactionSchema.safeParse({
      message_id: 'wamid.abc123',
      emoji: '👍',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing message_id', () => {
    const result = SendReactionSchema.safeParse({
      phoneNumber: '111',
      emoji: '👍',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing emoji', () => {
    const result = SendReactionSchema.safeParse({
      phoneNumber: '111',
      message_id: 'wamid.abc123',
    });
    expect(result.success).toBe(false);
  });

  it('should accept various emoji strings', () => {
    for (const emoji of ['❤️', '😂', '🎉', '👎', '✅']) {
      const result = SendReactionSchema.safeParse({
        phoneNumber: '111',
        message_id: 'mid',
        emoji,
      });
      expect(result.success).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// TypingIndicatorSchema
// ---------------------------------------------------------------------------

describe('TypingIndicatorSchema', () => {
  it('should accept composing state', () => {
    const result = TypingIndicatorSchema.safeParse({
      phoneNumber: '111',
      state: 'composing',
    });
    expect(result.success).toBe(true);
  });

  it('should accept paused state', () => {
    const result = TypingIndicatorSchema.safeParse({
      phoneNumber: '111',
      state: 'paused',
    });
    expect(result.success).toBe(true);
  });

  it('should reject invalid state value', () => {
    const result = TypingIndicatorSchema.safeParse({
      phoneNumber: '111',
      state: 'typing',
    });
    expect(result.success).toBe(false);
  });

  it('should accept optional message_id', () => {
    const result = TypingIndicatorSchema.safeParse({
      phoneNumber: '111',
      state: 'composing',
      message_id: 'wamid.xyz',
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.message_id).toBe('wamid.xyz');
    }
  });

  it('should accept without message_id', () => {
    const result = TypingIndicatorSchema.safeParse({
      phoneNumber: '111',
      state: 'composing',
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.message_id).toBeUndefined();
    }
  });

  it('should reject missing phoneNumber', () => {
    const result = TypingIndicatorSchema.safeParse({ state: 'composing' });
    expect(result.success).toBe(false);
  });

  it('should reject missing state', () => {
    const result = TypingIndicatorSchema.safeParse({ phoneNumber: '111' });
    expect(result.success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// ReadMessagesSchema
// ---------------------------------------------------------------------------

describe('ReadMessagesSchema', () => {
  it('should accept valid read messages request', () => {
    const result = ReadMessagesSchema.safeParse({
      phoneNumber: '111',
      message_ids: ['wamid.abc', 'wamid.def'],
    });
    expect(result.success).toBe(true);
  });

  it('should accept empty message_ids array', () => {
    const result = ReadMessagesSchema.safeParse({
      phoneNumber: '111',
      message_ids: [],
    });
    expect(result.success).toBe(true);
  });

  it('should accept single message_id', () => {
    const result = ReadMessagesSchema.safeParse({
      phoneNumber: '111',
      message_ids: ['wamid.abc'],
    });
    expect(result.success).toBe(true);
  });

  it('should reject missing phoneNumber', () => {
    const result = ReadMessagesSchema.safeParse({ message_ids: ['id1'] });
    expect(result.success).toBe(false);
  });

  it('should reject missing message_ids', () => {
    const result = ReadMessagesSchema.safeParse({ phoneNumber: '111' });
    expect(result.success).toBe(false);
  });

  it('should reject non-array message_ids', () => {
    const result = ReadMessagesSchema.safeParse({
      phoneNumber: '111',
      message_ids: 'wamid.abc',
    });
    expect(result.success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// EditMessageSchema
// ---------------------------------------------------------------------------

describe('EditMessageSchema', () => {
  it('should accept valid edit message request', () => {
    const result = EditMessageSchema.safeParse({
      phoneNumber: '111',
      message_id: 'wamid.abc',
      new_text: 'Updated message',
    });
    expect(result.success).toBe(true);
  });

  it('should reject empty new_text', () => {
    const result = EditMessageSchema.safeParse({
      phoneNumber: '111',
      message_id: 'wamid.abc',
      new_text: '',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing message_id', () => {
    const result = EditMessageSchema.safeParse({
      phoneNumber: '111',
      new_text: 'Updated',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing new_text', () => {
    const result = EditMessageSchema.safeParse({
      phoneNumber: '111',
      message_id: 'wamid.abc',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing phoneNumber', () => {
    const result = EditMessageSchema.safeParse({
      message_id: 'wamid.abc',
      new_text: 'Updated',
    });
    expect(result.success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// DeleteMessageSchema
// ---------------------------------------------------------------------------

describe('DeleteMessageSchema', () => {
  it('should accept valid delete message request', () => {
    const result = DeleteMessageSchema.safeParse({
      phoneNumber: '111',
      message_id: 'wamid.abc',
    });
    expect(result.success).toBe(true);
  });

  it('should reject missing phoneNumber', () => {
    const result = DeleteMessageSchema.safeParse({ message_id: 'wamid.abc' });
    expect(result.success).toBe(false);
  });

  it('should reject missing message_id', () => {
    const result = DeleteMessageSchema.safeParse({ phoneNumber: '111' });
    expect(result.success).toBe(false);
  });

  it('should reject empty object', () => {
    const result = DeleteMessageSchema.safeParse({});
    expect(result.success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Response Schemas
// ---------------------------------------------------------------------------

describe('SendTextResponseSchema', () => {
  it('should accept success with message_id', () => {
    const result = SendTextResponseSchema.safeParse({
      success: true,
      message_id: 'wamid.abc123',
    });
    expect(result.success).toBe(true);
  });

  it('should accept success without message_id', () => {
    const result = SendTextResponseSchema.safeParse({ success: true });
    expect(result.success).toBe(true);
  });

  it('should accept failure', () => {
    const result = SendTextResponseSchema.safeParse({ success: false });
    expect(result.success).toBe(true);
  });

  it('should reject missing success', () => {
    const result = SendTextResponseSchema.safeParse({ message_id: 'abc' });
    expect(result.success).toBe(false);
  });

  it('should reject non-boolean success', () => {
    const result = SendTextResponseSchema.safeParse({ success: 'yes' });
    expect(result.success).toBe(false);
  });
});

describe('SuccessResponseSchema', () => {
  it('should accept true success', () => {
    const result = SuccessResponseSchema.safeParse({ success: true });
    expect(result.success).toBe(true);
  });

  it('should accept false success', () => {
    const result = SuccessResponseSchema.safeParse({ success: false });
    expect(result.success).toBe(true);
  });

  it('should reject missing success', () => {
    const result = SuccessResponseSchema.safeParse({});
    expect(result.success).toBe(false);
  });
});

describe('ErrorResponseSchema', () => {
  it('should accept valid error response', () => {
    const result = ErrorResponseSchema.safeParse({ error: 'Something went wrong' });
    expect(result.success).toBe(true);
  });

  it('should reject missing error', () => {
    const result = ErrorResponseSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it('should reject non-string error', () => {
    const result = ErrorResponseSchema.safeParse({ error: 42 });
    expect(result.success).toBe(false);
  });
});

describe('HealthResponseSchema', () => {
  it('should accept valid health response', () => {
    const result = HealthResponseSchema.safeParse({
      status: 'ok',
      whatsapp_connected: true,
    });
    expect(result.success).toBe(true);
  });

  it('should accept disconnected state', () => {
    const result = HealthResponseSchema.safeParse({
      status: 'degraded',
      whatsapp_connected: false,
    });
    expect(result.success).toBe(true);
  });

  it('should reject missing status', () => {
    const result = HealthResponseSchema.safeParse({ whatsapp_connected: true });
    expect(result.success).toBe(false);
  });

  it('should reject missing whatsapp_connected', () => {
    const result = HealthResponseSchema.safeParse({ status: 'ok' });
    expect(result.success).toBe(false);
  });

  it('should reject non-boolean whatsapp_connected', () => {
    const result = HealthResponseSchema.safeParse({
      status: 'ok',
      whatsapp_connected: 'true',
    });
    expect(result.success).toBe(false);
  });
});
