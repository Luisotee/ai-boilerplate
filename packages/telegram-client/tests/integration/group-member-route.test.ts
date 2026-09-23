/**
 * POST /whatsapp/group-member — the live membership check the AI API makes
 * right before relaying a message into a Telegram group (send_group_message).
 *
 * A lookup error must surface as an error (the Python side fails closed on it),
 * never be translated into an answer here.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

vi.mock('../../src/services/telegram-api.js', () => ({
  sendText: vi.fn(),
  sendReaction: vi.fn(),
  sendChatAction: vi.fn(),
  isChatMember: vi.fn(),
}));

import { buildTestApp } from '../helpers/fastify.js';
import * as telegramApi from '../../src/services/telegram-api.js';
import { markBotReady, _resetBotReadyForTests } from '../../src/services/bot-state.js';

const mockIsChatMember = telegramApi.isChatMember as ReturnType<typeof vi.fn>;

describe('POST /whatsapp/group-member', () => {
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

  const post = (payload: object) =>
    app.inject({ method: 'POST', url: '/whatsapp/group-member', payload });

  it('returns 503 when the bot is not ready', async () => {
    const res = await post({ phoneNumber: 'tg:-1001', userJid: 'tg:5' });
    expect(res.statusCode).toBe(503);
    expect(mockIsChatMember).not.toHaveBeenCalled();
  });

  it('resolves both tg: ids and answers is_member', async () => {
    markBotReady();
    mockIsChatMember.mockResolvedValueOnce(true);
    const res = await post({ phoneNumber: 'tg:-1001234567890', userJid: 'tg:555' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ is_member: true });
    expect(mockIsChatMember).toHaveBeenCalledWith(-1001234567890, 555);
  });

  it('answers false for a non-member', async () => {
    markBotReady();
    mockIsChatMember.mockResolvedValueOnce(false);
    const res = await post({ phoneNumber: '-1001', userJid: '555' });
    expect(res.json()).toEqual({ is_member: false });
  });

  it('returns 400 for a malformed identifier', async () => {
    markBotReady();
    const res = await post({ phoneNumber: 'tg:-1001', userJid: 'not-a-number' });
    expect(res.statusCode).toBe(400);
    expect(mockIsChatMember).not.toHaveBeenCalled();
  });

  it('surfaces a lookup failure as 500 (never as "not a member")', async () => {
    markBotReady();
    mockIsChatMember.mockRejectedValueOnce(new Error('Bad Request: user not found'));
    const res = await post({ phoneNumber: 'tg:-1001', userJid: 'tg:5' });
    expect(res.statusCode).toBe(500);
  });
});
