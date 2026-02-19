/**
 * Integration tests for whatsapp-client messaging routes.
 *
 * Tests the HTTP routes via Fastify's inject() method with mocked
 * Baileys socket state, verifying correct status codes and response
 * bodies for all messaging endpoints.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

// ---------------------------------------------------------------------------
// Mocks — must be declared before any import that transitively loads the
// mocked modules.
// ---------------------------------------------------------------------------

// Mock the Baileys state service
vi.mock('../../src/services/baileys.js', () => ({
  getBaileysSocket: vi.fn(),
  isBaileysReady: vi.fn(),
  setBaileysSocket: vi.fn(),
}));

// Mock the JID normalizer — in production it calls sock.onWhatsApp() for
// phone numbers. In tests we just append the WhatsApp suffix.
vi.mock('../../src/utils/jid.js', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../src/utils/jid.js')>();
  return {
    ...original,
    normalizeJid: vi.fn(async (identifier: string) => {
      if (identifier.includes('@')) return identifier;
      return `${identifier}@s.whatsapp.net`;
    }),
  };
});

import { buildTestApp } from '../helpers/fastify.js';
import { makeMockSocket } from '../helpers/fixtures.js';
import { getBaileysSocket, isBaileysReady } from '../../src/services/baileys.js';

// Cast mocks for type-safe manipulation
const mockIsBaileysReady = isBaileysReady as ReturnType<typeof vi.fn>;
const mockGetBaileysSocket = getBaileysSocket as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('Messaging routes — /whatsapp/*', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  // =========================================================================
  // POST /whatsapp/send-text
  // =========================================================================
  describe('POST /whatsapp/send-text', () => {
    it('returns 503 when Baileys is not ready', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: 'Hello' },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    });

    it('returns 400 when required fields are missing', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999' }, // missing "text"
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 400 when text is empty string', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: '' },
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 with message_id on success', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: 'Hello' },
      });

      expect(res.statusCode).toBe(200);
      const body = res.json();
      expect(body.success).toBe(true);
      expect(body.message_id).toBeDefined();
      expect(mockSocket.sendMessage).toHaveBeenCalledOnce();
      expect(mockSocket.sendMessage).toHaveBeenCalledWith(
        '5511999999999@s.whatsapp.net',
        { text: 'Hello' }
      );
    });

    it('accepts a JID directly as phoneNumber', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999@s.whatsapp.net', text: 'Hey' },
      });

      expect(res.statusCode).toBe(200);
      expect(mockSocket.sendMessage).toHaveBeenCalledWith(
        '5511999999999@s.whatsapp.net',
        { text: 'Hey' }
      );
    });

    it('returns 500 when sendMessage throws a generic error', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.sendMessage.mockRejectedValueOnce(new Error('Connection lost'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999@s.whatsapp.net', text: 'Hello' },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to send message' });
    });
  });

  // =========================================================================
  // POST /whatsapp/send-reaction
  // =========================================================================
  describe('POST /whatsapp/send-reaction', () => {
    it('returns 503 when Baileys is not ready', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_123',
          emoji: '\uD83D\uDC4D',
        },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    });

    it('returns 400 when required fields are missing', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: '5511999999999' }, // missing message_id and emoji
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 on success and calls sendMessage with react payload', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_123',
          emoji: '\uD83D\uDC4D',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSocket.sendMessage).toHaveBeenCalledOnce();
      expect(mockSocket.sendMessage).toHaveBeenCalledWith(
        '5511999999999@s.whatsapp.net',
        {
          react: {
            text: '\uD83D\uDC4D',
            key: {
              remoteJid: '5511999999999@s.whatsapp.net',
              id: 'MSG_123',
              fromMe: false,
            },
          },
        }
      );
    });

    it('returns 500 when sendMessage throws', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.sendMessage.mockRejectedValueOnce(new Error('Network error'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_id: 'MSG_123',
          emoji: '\uD83D\uDC4D',
        },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to send reaction' });
    });
  });

  // =========================================================================
  // POST /whatsapp/typing
  // =========================================================================
  describe('POST /whatsapp/typing', () => {
    it('returns 503 when Baileys is not ready', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'composing' },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    });

    it('returns 400 with invalid typing state', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'invalid' },
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 for composing state', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'composing' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSocket.sendPresenceUpdate).toHaveBeenCalledWith(
        'composing',
        '5511999999999@s.whatsapp.net'
      );
    });

    it('returns 200 for paused state', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'paused' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSocket.sendPresenceUpdate).toHaveBeenCalledWith(
        'paused',
        '5511999999999@s.whatsapp.net'
      );
    });
  });

  // =========================================================================
  // POST /whatsapp/read-messages
  // =========================================================================
  describe('POST /whatsapp/read-messages', () => {
    it('returns 503 when Baileys is not ready', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/read-messages',
        payload: {
          phoneNumber: '5511999999999',
          message_ids: ['MSG_001'],
        },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    });

    it('returns 200 on success and calls readMessages with correct keys', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/read-messages',
        payload: {
          phoneNumber: '5511999999999',
          message_ids: ['MSG_001', 'MSG_002'],
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSocket.readMessages).toHaveBeenCalledOnce();
      expect(mockSocket.readMessages).toHaveBeenCalledWith([
        { remoteJid: '5511999999999@s.whatsapp.net', id: 'MSG_001', fromMe: false },
        { remoteJid: '5511999999999@s.whatsapp.net', id: 'MSG_002', fromMe: false },
      ]);
    });

    it('returns 500 when readMessages throws', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.readMessages.mockRejectedValueOnce(new Error('Read failed'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/read-messages',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_ids: ['MSG_001'],
        },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to mark messages as read' });
    });
  });
});
