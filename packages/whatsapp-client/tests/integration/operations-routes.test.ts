/**
 * Integration tests for whatsapp-client operations routes.
 *
 * Tests the edit-message and delete-message HTTP routes via Fastify's
 * inject() method with mocked Baileys socket state.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

vi.mock('../../src/services/baileys.js', () => ({
  getBaileysSocket: vi.fn(),
  isBaileysReady: vi.fn(),
  setBaileysSocket: vi.fn(),
}));

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

const mockIsBaileysReady = isBaileysReady as ReturnType<typeof vi.fn>;
const mockGetBaileysSocket = getBaileysSocket as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('Operations routes — /whatsapp/*', () => {
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
  // POST /whatsapp/edit-message
  // =========================================================================
  describe('POST /whatsapp/edit-message', () => {
    it('returns 503 when Baileys is not ready', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/edit-message',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_001',
          new_text: 'Updated text',
        },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    });

    it('returns 400 when required fields are missing', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/edit-message',
        payload: { phoneNumber: '5511999999999' }, // missing message_id and new_text
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 400 when new_text is empty', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/edit-message',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_001',
          new_text: '',
        },
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 on success and calls sendMessage with edit payload', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/edit-message',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_001',
          new_text: 'Corrected text',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSocket.sendMessage).toHaveBeenCalledOnce();
      expect(mockSocket.sendMessage).toHaveBeenCalledWith(
        '5511999999999@s.whatsapp.net',
        {
          text: 'Corrected text',
          edit: {
            remoteJid: '5511999999999@s.whatsapp.net',
            id: 'MSG_001',
            fromMe: true,
          },
        }
      );
    });

    it('returns 500 when sendMessage throws a generic error', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.sendMessage.mockRejectedValueOnce(new Error('Unknown failure'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/edit-message',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_id: 'MSG_001',
          new_text: 'Updated',
        },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to edit message' });
    });

    it('returns 400 when message is too old to edit', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.sendMessage.mockRejectedValueOnce(new Error('Message too old to edit'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/edit-message',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_id: 'MSG_001',
          new_text: 'Updated',
        },
      });

      expect(res.statusCode).toBe(400);
      expect(res.json()).toEqual({
        error: 'Cannot edit message (too old or not yours)',
      });
    });
  });

  // =========================================================================
  // DELETE /whatsapp/delete-message
  // =========================================================================
  describe('DELETE /whatsapp/delete-message', () => {
    it('returns 503 when Baileys is not ready', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({
        method: 'DELETE',
        url: '/whatsapp/delete-message',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_001',
        },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp not connected' });
    });

    it('returns 400 when required fields are missing', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({
        method: 'DELETE',
        url: '/whatsapp/delete-message',
        payload: { phoneNumber: '5511999999999' }, // missing message_id
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 on success and calls sendMessage with delete payload', async () => {
      const mockSocket = makeMockSocket();
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'DELETE',
        url: '/whatsapp/delete-message',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_001',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSocket.sendMessage).toHaveBeenCalledOnce();
      expect(mockSocket.sendMessage).toHaveBeenCalledWith(
        '5511999999999@s.whatsapp.net',
        {
          delete: {
            remoteJid: '5511999999999@s.whatsapp.net',
            id: 'MSG_001',
            fromMe: true,
          },
        }
      );
    });

    it('returns 500 when sendMessage throws a generic error', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.sendMessage.mockRejectedValueOnce(new Error('Server error'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'DELETE',
        url: '/whatsapp/delete-message',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_id: 'MSG_001',
        },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to delete message' });
    });

    it('returns 400 when message is too old to delete', async () => {
      const mockSocket = makeMockSocket();
      mockSocket.sendMessage.mockRejectedValueOnce(new Error('Message too old'));
      mockIsBaileysReady.mockReturnValue(true);
      mockGetBaileysSocket.mockReturnValue(mockSocket);

      const res = await app.inject({
        method: 'DELETE',
        url: '/whatsapp/delete-message',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_id: 'MSG_001',
        },
      });

      expect(res.statusCode).toBe(400);
      expect(res.json()).toEqual({
        error: 'Cannot delete message (too old or not yours)',
      });
    });
  });
});
