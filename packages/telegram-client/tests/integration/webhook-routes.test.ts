/**
 * Integration test for the /webhook route's secret-token verification.
 *
 * grammY's webhookCallback(bot, 'fastify', { secretToken }) compares the
 * `X-Telegram-Bot-Api-Secret-Token` header against the configured secret.
 * TELEGRAM_WEBHOOK_SECRET is set to "test-webhook-secret" in tests/setup.ts.
 *
 * webhookCallback also calls `bot.init()` implicitly on first dispatch, so
 * we stub both bot.init() and bot.handleUpdate() to keep the test hermetic.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

import { bot } from '../../src/bot.js';
import { buildTestApp } from '../helpers/fastify.js';

const WEBHOOK_SECRET = 'test-webhook-secret';

// Minimal valid Telegram Update payload. Most fields are optional; we just
// need something grammY accepts as a valid JSON body.
const minimalUpdate = {
  update_id: 1,
  message: {
    message_id: 1,
    date: Math.floor(Date.now() / 1000),
    chat: { id: 1, type: 'private' },
    from: { id: 1, is_bot: false, first_name: 'Test' },
    text: 'hello',
  },
};

describe('POST /webhook — Telegram secret-token verification', () => {
  let app: FastifyInstance;
  let handleUpdateSpy: ReturnType<typeof vi.spyOn>;
  let initSpy: ReturnType<typeof vi.spyOn>;

  beforeAll(async () => {
    // bot.init() uses getMe() against a fake token; stub it.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    initSpy = vi.spyOn(bot, 'init' as any).mockResolvedValue(undefined as never);
    handleUpdateSpy = vi.spyOn(bot, 'handleUpdate').mockResolvedValue(undefined);
    app = await buildTestApp({ includeWebhook: true });
  });

  afterAll(async () => {
    initSpy.mockRestore();
    handleUpdateSpy.mockRestore();
    await app.close();
  });

  beforeEach(() => {
    handleUpdateSpy.mockClear();
  });

  it('returns 401 when the secret token header is missing', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/webhook',
      payload: minimalUpdate,
    });

    expect(res.statusCode).toBe(401);
    expect(handleUpdateSpy).not.toHaveBeenCalled();
  });

  it('returns 401 when the secret token header is wrong', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/webhook',
      payload: minimalUpdate,
      headers: { 'x-telegram-bot-api-secret-token': 'wrong-secret' },
    });

    expect(res.statusCode).toBe(401);
    expect(handleUpdateSpy).not.toHaveBeenCalled();
  });

  it('accepts the update and dispatches to grammY when the secret matches', async () => {
    const res = await app.inject({
      method: 'POST',
      url: '/webhook',
      payload: minimalUpdate,
      headers: { 'x-telegram-bot-api-secret-token': WEBHOOK_SECRET },
    });

    expect(res.statusCode).toBe(200);
    expect(handleUpdateSpy).toHaveBeenCalledOnce();
    expect(handleUpdateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ update_id: 1 }),
      expect.anything()
    );
  });
});
