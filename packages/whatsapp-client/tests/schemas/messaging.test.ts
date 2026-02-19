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

describe('SendTextSchema', () => {
  it('should accept valid input with required fields', () => {
    const input = { phoneNumber: '5511999999999', text: 'Hello!' };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.phoneNumber).toBe('5511999999999');
      expect(result.data.text).toBe('Hello!');
      expect(result.data.quoted_message_id).toBeUndefined();
    }
  });

  it('should accept valid input with optional quoted_message_id', () => {
    const input = {
      phoneNumber: '5511999999999',
      text: 'Reply!',
      quoted_message_id: 'msg_123',
    };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.quoted_message_id).toBe('msg_123');
    }
  });

  it('should reject empty text', () => {
    const input = { phoneNumber: '5511999999999', text: '' };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing phoneNumber', () => {
    const input = { text: 'Hello!' };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing text', () => {
    const input = { phoneNumber: '5511999999999' };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject non-string phoneNumber', () => {
    const input = { phoneNumber: 5511999999999, text: 'Hello!' };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject non-string text', () => {
    const input = { phoneNumber: '5511999999999', text: 123 };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept text with min length of 1', () => {
    const input = { phoneNumber: '5511999999999', text: 'a' };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should accept long text', () => {
    const input = { phoneNumber: '5511999999999', text: 'x'.repeat(10000) };
    const result = SendTextSchema.safeParse(input);

    expect(result.success).toBe(true);
  });
});

describe('SendReactionSchema', () => {
  it('should accept valid reaction input', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'BAE5F2B8C1234567',
      emoji: '\u{1F44D}',
    };
    const result = SendReactionSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.phoneNumber).toBe('5511999999999');
      expect(result.data.message_id).toBe('BAE5F2B8C1234567');
      expect(result.data.emoji).toBe('\u{1F44D}');
    }
  });

  it('should reject missing phoneNumber', () => {
    const input = { message_id: 'BAE5F2B8C1234567', emoji: '\u{1F44D}' };
    const result = SendReactionSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing message_id', () => {
    const input = { phoneNumber: '5511999999999', emoji: '\u{1F44D}' };
    const result = SendReactionSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing emoji', () => {
    const input = { phoneNumber: '5511999999999', message_id: 'BAE5F2B8C1234567' };
    const result = SendReactionSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept any string as emoji', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'msg_123',
      emoji: 'text',
    };
    const result = SendReactionSchema.safeParse(input);

    expect(result.success).toBe(true);
  });
});

describe('TypingIndicatorSchema', () => {
  it('should accept composing state', () => {
    const input = { phoneNumber: '5511999999999', state: 'composing' };
    const result = TypingIndicatorSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.state).toBe('composing');
    }
  });

  it('should accept paused state', () => {
    const input = { phoneNumber: '5511999999999', state: 'paused' };
    const result = TypingIndicatorSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.state).toBe('paused');
    }
  });

  it('should reject invalid state value', () => {
    const input = { phoneNumber: '5511999999999', state: 'typing' };
    const result = TypingIndicatorSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing state', () => {
    const input = { phoneNumber: '5511999999999' };
    const result = TypingIndicatorSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing phoneNumber', () => {
    const input = { state: 'composing' };
    const result = TypingIndicatorSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject empty state string', () => {
    const input = { phoneNumber: '5511999999999', state: '' };
    const result = TypingIndicatorSchema.safeParse(input);

    expect(result.success).toBe(false);
  });
});

describe('ReadMessagesSchema', () => {
  it('should accept valid input with array of message IDs', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_ids: ['msg_1', 'msg_2', 'msg_3'],
    };
    const result = ReadMessagesSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.message_ids).toHaveLength(3);
    }
  });

  it('should accept empty array of message IDs', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_ids: [],
    };
    const result = ReadMessagesSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.message_ids).toHaveLength(0);
    }
  });

  it('should accept single message ID', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_ids: ['msg_1'],
    };
    const result = ReadMessagesSchema.safeParse(input);

    expect(result.success).toBe(true);
  });

  it('should reject missing message_ids', () => {
    const input = { phoneNumber: '5511999999999' };
    const result = ReadMessagesSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject non-array message_ids', () => {
    const input = { phoneNumber: '5511999999999', message_ids: 'msg_1' };
    const result = ReadMessagesSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject array with non-string elements', () => {
    const input = { phoneNumber: '5511999999999', message_ids: [123, 456] };
    const result = ReadMessagesSchema.safeParse(input);

    expect(result.success).toBe(false);
  });
});

describe('EditMessageSchema', () => {
  it('should accept valid edit input', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'BAE5F2B8C1234567',
      new_text: 'Updated message',
    };
    const result = EditMessageSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.new_text).toBe('Updated message');
    }
  });

  it('should reject empty new_text', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'BAE5F2B8C1234567',
      new_text: '',
    };
    const result = EditMessageSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing message_id', () => {
    const input = {
      phoneNumber: '5511999999999',
      new_text: 'Updated',
    };
    const result = EditMessageSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing new_text', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'msg_123',
    };
    const result = EditMessageSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should accept new_text with minimum length of 1', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'msg_123',
      new_text: 'x',
    };
    const result = EditMessageSchema.safeParse(input);

    expect(result.success).toBe(true);
  });
});

describe('DeleteMessageSchema', () => {
  it('should accept valid delete input', () => {
    const input = {
      phoneNumber: '5511999999999',
      message_id: 'BAE5F2B8C1234567',
    };
    const result = DeleteMessageSchema.safeParse(input);

    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.phoneNumber).toBe('5511999999999');
      expect(result.data.message_id).toBe('BAE5F2B8C1234567');
    }
  });

  it('should reject missing phoneNumber', () => {
    const input = { message_id: 'BAE5F2B8C1234567' };
    const result = DeleteMessageSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject missing message_id', () => {
    const input = { phoneNumber: '5511999999999' };
    const result = DeleteMessageSchema.safeParse(input);

    expect(result.success).toBe(false);
  });

  it('should reject empty object', () => {
    const result = DeleteMessageSchema.safeParse({});

    expect(result.success).toBe(false);
  });
});

describe('Response Schemas', () => {
  describe('SendTextResponseSchema', () => {
    it('should accept success with message_id', () => {
      const input = { success: true, message_id: 'msg_123' };
      const result = SendTextResponseSchema.safeParse(input);

      expect(result.success).toBe(true);
    });

    it('should accept success without message_id', () => {
      const input = { success: false };
      const result = SendTextResponseSchema.safeParse(input);

      expect(result.success).toBe(true);
    });

    it('should reject missing success', () => {
      const input = { message_id: 'msg_123' };
      const result = SendTextResponseSchema.safeParse(input);

      expect(result.success).toBe(false);
    });

    it('should reject non-boolean success', () => {
      const input = { success: 'true' };
      const result = SendTextResponseSchema.safeParse(input);

      expect(result.success).toBe(false);
    });
  });

  describe('SuccessResponseSchema', () => {
    it('should accept valid success response', () => {
      const result = SuccessResponseSchema.safeParse({ success: true });
      expect(result.success).toBe(true);
    });

    it('should accept false success', () => {
      const result = SuccessResponseSchema.safeParse({ success: false });
      expect(result.success).toBe(true);
    });

    it('should reject missing success field', () => {
      const result = SuccessResponseSchema.safeParse({});
      expect(result.success).toBe(false);
    });
  });

  describe('ErrorResponseSchema', () => {
    it('should accept valid error response', () => {
      const result = ErrorResponseSchema.safeParse({ error: 'Something went wrong' });
      expect(result.success).toBe(true);
    });

    it('should reject missing error field', () => {
      const result = ErrorResponseSchema.safeParse({});
      expect(result.success).toBe(false);
    });

    it('should reject non-string error', () => {
      const result = ErrorResponseSchema.safeParse({ error: 404 });
      expect(result.success).toBe(false);
    });
  });

  describe('HealthResponseSchema', () => {
    it('should accept valid health response', () => {
      const input = { status: 'ok', whatsapp_connected: true };
      const result = HealthResponseSchema.safeParse(input);

      expect(result.success).toBe(true);
    });

    it('should accept disconnected health response', () => {
      const input = { status: 'degraded', whatsapp_connected: false };
      const result = HealthResponseSchema.safeParse(input);

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
  });
});
