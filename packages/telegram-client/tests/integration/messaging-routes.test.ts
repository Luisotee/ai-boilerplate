/**
 * Integration tests for telegram-client messaging routes.
 *
 * Tests the HTTP routes via Fastify's inject() method with mocked
 * telegram-api and bot-state, verifying correct status codes and response
 * bodies for all messaging endpoints.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

// ---------------------------------------------------------------------------
// Mocks — must be declared before any import that transitively loads them.
// ---------------------------------------------------------------------------

vi.mock('../../src/services/telegram-api.js', () => ({
  sendText: vi.fn().mockResolvedValue(42),
  sendReaction: vi.fn().mockResolvedValue(undefined),
  sendChatAction: vi.fn().mockResolvedValue(undefined),
}));

import { buildTestApp } from '../helpers/fastify.js';
import * as telegramApi from '../../src/services/telegram-api.js';
import { markBotReady, _resetBotReadyForTests } from '../../src/services/bot-state.js';

const mockSendText = telegramApi.sendText as ReturnType<typeof vi.fn>;
const mockSendReaction = telegramApi.sendReaction as ReturnType<typeof vi.fn>;
const mockSendChatAction = telegramApi.sendChatAction as ReturnType<typeof vi.fn>;

describe('Telegram messaging routes — /whatsapp/*', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    _resetBotReadyForTests();
  });

  // =========================================================================
  // POST /whatsapp/send-text
  // =========================================================================
  describe('POST /whatsapp/send-text', () => {
    it('returns 503 when the bot is not ready', async () => {
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345', text: 'Hello' },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'Telegram bot not ready' });
      expect(mockSendText).not.toHaveBeenCalled();
    });

    it('returns 400 when required fields are missing', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345' }, // missing text
      });
      expect(res.statusCode).toBe(400);
    });

    it('returns 400 when text is empty', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345', text: '' },
      });
      expect(res.statusCode).toBe(400);
    });

    it('returns 200 with message_id on success and converts tg: JID', async () => {
      markBotReady();
      mockSendText.mockResolvedValueOnce(777);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345', text: 'Hello Telegram' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true, message_id: '777' });
      expect(mockSendText).toHaveBeenCalledWith(12345, 'Hello Telegram', undefined);
    });

    it('parses supergroup tg:-100... JIDs to negative chat id', async () => {
      markBotReady();
      mockSendText.mockResolvedValueOnce(1);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:-1001234567890', text: 'Hi group' },
      });

      expect(res.statusCode).toBe(200);
      expect(mockSendText).toHaveBeenCalledWith(-1001234567890, 'Hi group', undefined);
    });

    it('passes quoted_message_id as reply target', async () => {
      markBotReady();
      mockSendText.mockResolvedValueOnce(99);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345', text: 'Reply', quoted_message_id: '50' },
      });

      expect(res.statusCode).toBe(200);
      expect(mockSendText).toHaveBeenCalledWith(12345, 'Reply', 50);
    });

    it('accepts bare numeric chat id (no tg: prefix)', async () => {
      markBotReady();
      mockSendText.mockResolvedValueOnce(1);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '12345', text: 'Direct numeric' },
      });

      expect(res.statusCode).toBe(200);
      expect(mockSendText).toHaveBeenCalledWith(12345, 'Direct numeric', undefined);
    });

    it('returns 400 when the chat identifier is invalid (non-numeric, non-tg)', async () => {
      markBotReady();

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'not-a-valid-id', text: 'Hello' },
      });

      expect(res.statusCode).toBe(400);
      expect(res.json().error).toMatch(/invalid chat identifier/i);
      expect(mockSendText).not.toHaveBeenCalled();
    });

    it('returns 400 when quoted_message_id is not numeric', async () => {
      markBotReady();

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345', text: 'Reply', quoted_message_id: 'abc' },
      });

      expect(res.statusCode).toBe(400);
      expect(res.json()).toEqual({ error: 'Invalid quoted_message_id' });
      expect(mockSendText).not.toHaveBeenCalled();
    });

    it('returns 500 when telegram-api throws', async () => {
      markBotReady();
      mockSendText.mockRejectedValueOnce(new Error('grammY boom'));

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: 'tg:12345', text: 'Hi' },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'grammY boom' });
    });
  });

  // =========================================================================
  // POST /whatsapp/send-reaction
  // =========================================================================
  describe('POST /whatsapp/send-reaction', () => {
    it('returns 503 when the bot is not ready', async () => {
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: 'tg:12345', message_id: '1', emoji: '👍' },
      });

      expect(res.statusCode).toBe(503);
      expect(mockSendReaction).not.toHaveBeenCalled();
    });

    it('returns 400 when message_id is not finite', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: 'tg:12345', message_id: 'not-a-number', emoji: '👍' },
      });
      expect(res.statusCode).toBe(400);
      expect(res.json()).toEqual({ error: 'Invalid message_id' });
    });

    it('returns 200 on success and calls telegram-api.sendReaction', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: 'tg:12345', message_id: '50', emoji: '❌' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSendReaction).toHaveBeenCalledWith(12345, 50, '❌');
    });

    it('returns 500 when telegram-api.sendReaction throws', async () => {
      markBotReady();
      mockSendReaction.mockRejectedValueOnce(new Error('reaction API broken'));

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: 'tg:12345', message_id: '1', emoji: '👍' },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'reaction API broken' });
    });

    it('returns 400 when the chat identifier is invalid', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: 'not-a-valid-id', message_id: '1', emoji: '👍' },
      });
      expect(res.statusCode).toBe(400);
      expect(res.json().error).toMatch(/invalid chat identifier/i);
      expect(mockSendReaction).not.toHaveBeenCalled();
    });
  });

  // =========================================================================
  // POST /whatsapp/typing
  // =========================================================================
  describe('POST /whatsapp/typing', () => {
    it('returns 503 when the bot is not ready', async () => {
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: 'tg:12345', state: 'composing' },
      });

      expect(res.statusCode).toBe(503);
      expect(mockSendChatAction).not.toHaveBeenCalled();
    });

    it('returns 200 for composing and fires typing chat action', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: 'tg:12345', state: 'composing' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSendChatAction).toHaveBeenCalledWith(12345, 'typing');
    });

    it('returns 200 for paused state as a no-op (no API call)', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: 'tg:12345', state: 'paused' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSendChatAction).not.toHaveBeenCalled();
    });

    it('returns 400 for an invalid state', async () => {
      markBotReady();
      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: 'tg:12345', state: 'typing' },
      });
      expect(res.statusCode).toBe(400);
    });

    it('returns 500 when sendChatAction throws', async () => {
      markBotReady();
      mockSendChatAction.mockRejectedValueOnce(new Error('chat action failed'));

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: 'tg:12345', state: 'composing' },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'chat action failed' });
    });
  });
});
