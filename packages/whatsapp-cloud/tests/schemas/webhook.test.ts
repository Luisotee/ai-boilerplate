import { describe, it, expect } from 'vitest';
import { WebhookVerifySchema, WebhookBodySchema } from '../../src/schemas/webhook.js';

// ---------------------------------------------------------------------------
// WebhookVerifySchema
// ---------------------------------------------------------------------------

describe('WebhookVerifySchema', () => {
  it('should accept valid webhook verification query', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.mode': 'subscribe',
      'hub.verify_token': 'my_verify_token',
      'hub.challenge': '1234567890',
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data['hub.mode']).toBe('subscribe');
      expect(result.data['hub.verify_token']).toBe('my_verify_token');
      expect(result.data['hub.challenge']).toBe('1234567890');
    }
  });

  it('should reject missing hub.mode', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.verify_token': 'token',
      'hub.challenge': '123',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing hub.verify_token', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.mode': 'subscribe',
      'hub.challenge': '123',
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing hub.challenge', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.mode': 'subscribe',
      'hub.verify_token': 'token',
    });
    expect(result.success).toBe(false);
  });

  it('should reject empty object', () => {
    const result = WebhookVerifySchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it('should accept any string for hub.mode (not restricted to subscribe)', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.mode': 'unsubscribe',
      'hub.verify_token': 'token',
      'hub.challenge': '123',
    });
    expect(result.success).toBe(true);
  });

  it('should accept numeric challenge string', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.mode': 'subscribe',
      'hub.verify_token': 'token',
      'hub.challenge': '9876543210',
    });
    expect(result.success).toBe(true);
  });

  it('should reject non-string hub.challenge', () => {
    const result = WebhookVerifySchema.safeParse({
      'hub.mode': 'subscribe',
      'hub.verify_token': 'token',
      'hub.challenge': 1234567890,
    });
    expect(result.success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// WebhookBodySchema
// ---------------------------------------------------------------------------

describe('WebhookBodySchema', () => {
  // ---- Minimal valid payloads ----

  it('should accept minimal valid webhook body with messages', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: {
                  phone_number_id: '12345',
                  display_phone_number: '+15550001234',
                },
                messages: [
                  {
                    from: '16505551234',
                    id: 'wamid.HBg123',
                    timestamp: '1700000000',
                    type: 'text',
                  },
                ],
              },
              field: 'messages',
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept webhook body with only statuses (no messages)', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                statuses: [
                  {
                    id: 'wamid.xyz',
                    status: 'delivered',
                    timestamp: '1700000000',
                    recipient_id: '111',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept webhook body with contacts', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                contacts: [
                  {
                    profile: { name: 'John Doe' },
                    wa_id: '16505551234',
                  },
                ],
                messages: [
                  {
                    from: '16505551234',
                    id: 'wamid.HBg123',
                    timestamp: '1700000000',
                    type: 'text',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept webhook body with contact without profile name', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                contacts: [
                  {
                    wa_id: '16505551234',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  // ---- Optional fields ----

  it('should accept value without messaging_product (optional)', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {},
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept value without metadata (optional)', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept value without field property (optional)', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {},
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  // ---- Messages passthrough ----

  it('should pass through additional message fields (passthrough on messages)', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messages: [
                  {
                    from: '16505551234',
                    id: 'wamid.HBg123',
                    timestamp: '1700000000',
                    type: 'text',
                    text: { body: 'Hello!' },
                    context: { from: '15550001234', id: 'wamid.prev' },
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success) {
      const msg = result.data.entry[0].changes[0].value.messages?.[0];
      expect(msg).toBeDefined();
      // passthrough should preserve extra fields
      expect((msg as Record<string, unknown>).text).toEqual({ body: 'Hello!' });
      expect((msg as Record<string, unknown>).context).toEqual({
        from: '15550001234',
        id: 'wamid.prev',
      });
    }
  });

  // ---- Validation failures ----

  it('should reject missing object field', () => {
    const result = WebhookBodySchema.safeParse({
      entry: [
        {
          id: 'WABA_ID',
          changes: [{ value: {} }],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject missing entry field', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
    });
    expect(result.success).toBe(false);
  });

  it('should reject entry without id', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          changes: [{ value: {} }],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject entry without changes', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject changes without value', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [{ field: 'messages' }],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject empty object', () => {
    const result = WebhookBodySchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it('should reject non-string entry id', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 12345,
          changes: [{ value: {} }],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  // ---- Message required fields ----

  it('should reject message missing from field', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messages: [
                  {
                    id: 'wamid.HBg123',
                    timestamp: '1700000000',
                    type: 'text',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject message missing id field', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messages: [
                  {
                    from: '16505551234',
                    timestamp: '1700000000',
                    type: 'text',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject message missing timestamp field', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messages: [
                  {
                    from: '16505551234',
                    id: 'wamid.HBg123',
                    type: 'text',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject message missing type field', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messages: [
                  {
                    from: '16505551234',
                    id: 'wamid.HBg123',
                    timestamp: '1700000000',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  // ---- Status required fields ----

  it('should reject status missing required fields', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                statuses: [
                  {
                    id: 'wamid.xyz',
                    // missing status, timestamp, recipient_id
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should accept valid status with all required fields', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                statuses: [
                  {
                    id: 'wamid.xyz',
                    status: 'read',
                    timestamp: '1700000000',
                    recipient_id: '16505551234',
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  // ---- Metadata required fields ----

  it('should reject metadata missing phone_number_id', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                metadata: {
                  display_phone_number: '+15550001234',
                },
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  it('should reject metadata missing display_phone_number', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                metadata: {
                  phone_number_id: '12345',
                },
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  // ---- Contact required fields ----

  it('should reject contact missing wa_id', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                contacts: [
                  {
                    profile: { name: 'Jane' },
                  },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(false);
  });

  // ---- Complex / realistic payloads ----

  it('should accept a full realistic webhook payload', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_123456',
          changes: [
            {
              value: {
                messaging_product: 'whatsapp',
                metadata: {
                  phone_number_id: '987654321',
                  display_phone_number: '+15550009876',
                },
                contacts: [
                  {
                    profile: { name: 'Alice Johnson' },
                    wa_id: '16505551234',
                  },
                ],
                messages: [
                  {
                    from: '16505551234',
                    id: 'wamid.HBgNMTY1MDU1NTEyMzQVAgASGBQzQUEwRTIyQjdENkE0QjA2RkYA',
                    timestamp: '1700000000',
                    type: 'text',
                    text: { body: 'Hello, can you help me?' },
                  },
                ],
              },
              field: 'messages',
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept empty entry array', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [],
    });
    expect(result.success).toBe(true);
  });

  it('should accept empty changes array', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [],
        },
      ],
    });
    expect(result.success).toBe(true);
  });

  it('should accept multiple messages in one change', () => {
    const result = WebhookBodySchema.safeParse({
      object: 'whatsapp_business_account',
      entry: [
        {
          id: 'WABA_ID',
          changes: [
            {
              value: {
                messages: [
                  { from: '111', id: 'id1', timestamp: '123', type: 'text' },
                  { from: '222', id: 'id2', timestamp: '456', type: 'image' },
                ],
              },
            },
          ],
        },
      ],
    });
    expect(result.success).toBe(true);
    if (result.success) {
      expect(result.data.entry[0].changes[0].value.messages).toHaveLength(2);
    }
  });
});
