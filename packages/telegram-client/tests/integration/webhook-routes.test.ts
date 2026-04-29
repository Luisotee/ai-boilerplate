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
import { GrammyError } from 'grammy';

import { bot } from '../../src/bot.js';
import { logger } from '../../src/logger.js';
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

describe('POST /webhook — error path returns 500 with structured logging', () => {
  let app: FastifyInstance;
  let initSpy: ReturnType<typeof vi.spyOn>;
  let handleUpdateSpy: ReturnType<typeof vi.spyOn>;
  let loggerErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeAll(async () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    initSpy = vi.spyOn(bot, 'init' as any).mockResolvedValue(undefined as never);
    handleUpdateSpy = vi.spyOn(bot, 'handleUpdate');
    loggerErrorSpy = vi.spyOn(logger, 'error').mockImplementation(() => undefined);
    app = await buildTestApp({ includeWebhook: true });
  });

  afterAll(async () => {
    initSpy.mockRestore();
    handleUpdateSpy.mockRestore();
    loggerErrorSpy.mockRestore();
    await app.close();
  });

  beforeEach(() => {
    handleUpdateSpy.mockReset();
    loggerErrorSpy.mockClear();
  });

  it('returns 500 and logs Telegram API rejection with update context on GrammyError', async () => {
    const grammyError = new GrammyError(
      'Telegram server error',
      { ok: false, error_code: 403, description: 'Forbidden: bot was kicked' },
      'sendMessage',
      {}
    );
    handleUpdateSpy.mockRejectedValueOnce(grammyError);

    const res = await app.inject({
      method: 'POST',
      url: '/webhook',
      payload: minimalUpdate,
      headers: { 'x-telegram-bot-api-secret-token': WEBHOOK_SECRET },
    });

    expect(res.statusCode).toBe(500);
    expect(loggerErrorSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        err: grammyError,
        method: 'sendMessage',
        description: expect.stringContaining('Forbidden'),
        updateId: 1,
        chatId: 1,
      }),
      'Telegram API rejected request'
    );
  });

  it('returns 500 and logs unhandled errors with update context', async () => {
    const err = new Error('AI API down');
    handleUpdateSpy.mockRejectedValueOnce(err);

    const res = await app.inject({
      method: 'POST',
      url: '/webhook',
      payload: minimalUpdate,
      headers: { 'x-telegram-bot-api-secret-token': WEBHOOK_SECRET },
    });

    expect(res.statusCode).toBe(500);
    expect(loggerErrorSpy).toHaveBeenCalledWith(
      expect.objectContaining({ err, updateId: 1, chatId: 1 }),
      'Unhandled error in bot handler'
    );
  });
});
