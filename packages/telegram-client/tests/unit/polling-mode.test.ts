/**
 * TELEGRAM_MODE=polling (ported from curupira's bot-error-boundary tests, plus
 * the drain and startup contract) — and proof that webhook mode is unchanged.
 *
 * Polling: a throwing handler must never propagate out of `bot.handleUpdate`
 * (it would reach the runner's sink and, as an unhandled rejection, main.ts's
 * process.exit(1)). Webhook: it MUST propagate, so routes/webhook.ts logs it
 * and Fastify answers 500 and Telegram retries.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Update, UserFromGetMe } from 'grammy/types';

vi.mock('../../src/logger.js', () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn(), fatal: vi.fn() },
}));

vi.mock('../../src/instrument.js', () => ({
  Sentry: { captureException: vi.fn() },
}));

const runMock = vi.fn();
vi.mock('@grammyjs/runner', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@grammyjs/runner')>()),
  run: (...args: unknown[]) => runMock(...args),
}));

const BOT_INFO = {
  id: 1,
  is_bot: true,
  first_name: 'Assistant',
  username: 'MyBot',
} as UserFromGetMe;

function textUpdate(text: string, updateId = 1): Update {
  return {
    update_id: updateId,
    message: {
      message_id: 100,
      date: 0,
      chat: { id: 555, type: 'private', first_name: 'Ana' },
      from: { id: 555, is_bot: false, first_name: 'Ana' },
      text,
    },
  } as Update;
}

async function freshBot(mode: 'webhook' | 'polling') {
  vi.resetModules();
  vi.stubEnv('TELEGRAM_MODE', mode);
  const { logger } = await import('../../src/logger.js');
  const { Sentry } = await import('../../src/instrument.js');
  const { bot } = await import('../../src/bot.js');
  bot.botInfo = BOT_INFO;
  return {
    bot,
    logError: logger.error as unknown as ReturnType<typeof vi.fn>,
    captureException: Sentry.captureException as unknown as ReturnType<typeof vi.fn>,
  };
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.unstubAllEnvs());

describe('polling mode: error boundary', () => {
  it('swallows a sync handler throw and reports it with context', async () => {
    const { bot, logError, captureException } = await freshBot('polling');
    bot.on('message:text', () => {
      throw new Error('handler exploded');
    });

    await expect(bot.handleUpdate(textUpdate('boom', 3))).resolves.toBeUndefined();
    expect(captureException).toHaveBeenCalled();
    expect(logError.mock.calls.at(-1)![0]).toMatchObject({ chatId: 555, fromId: 555, updateId: 3 });
  });

  it('survives an async rejection', async () => {
    const { bot } = await freshBot('polling');
    bot.on('message:text', async () => {
      await Promise.resolve();
      throw new Error('async boom');
    });
    await expect(bot.handleUpdate(textUpdate('boom'))).resolves.toBeUndefined();
  });

  it('lets a successful handler run normally', async () => {
    const { bot, logError } = await freshBot('polling');
    const handler = vi.fn();
    bot.on('message:text', handler);
    await bot.handleUpdate(textUpdate('hi'));
    expect(handler).toHaveBeenCalledTimes(1);
    expect(logError).not.toHaveBeenCalled();
  });
});

describe('webhook mode: unchanged', () => {
  it('still propagates handler errors (routes/webhook.ts logs + 500 → Telegram retries)', async () => {
    const { bot, captureException } = await freshBot('webhook');
    bot.on('message:text', () => {
      throw new Error('handler exploded');
    });
    await expect(bot.handleUpdate(textUpdate('boom'))).rejects.toThrow();
    expect(captureException).not.toHaveBeenCalled();
  });
});

describe('startLongPolling', () => {
  it('deletes the webhook BEFORE starting the runner, with bounded retries', async () => {
    const { bot } = await freshBot('polling');
    const calls: string[] = [];
    const deleteWebhook = vi
      .spyOn(bot.api, 'deleteWebhook')
      .mockImplementation(async () => (calls.push('deleteWebhook'), true));
    runMock.mockImplementation(() => (calls.push('run'), { task: () => undefined }));

    const { startLongPolling } = await import('../../src/polling.js');
    await startLongPolling();

    expect(calls).toEqual(['deleteWebhook', 'run']);
    expect(deleteWebhook).toHaveBeenCalledWith({ drop_pending_updates: true });
    const [runBot, options] = runMock.mock.calls[0];
    expect(runBot).toBe(bot);
    expect(options.runner.fetch.allowed_updates).toEqual(['message']);
    expect(options.runner.maxRetryTime).toBeGreaterThan(0);
    expect(options.runner.maxRetryTime).toBeLessThan(60 * 60_000);
  });
});

describe('drainInFlight / stopLongPolling', () => {
  const log = () => ({ info: vi.fn(), warn: vi.fn() });

  it('returns at once when nothing is in flight', async () => {
    const { drainInFlight } = await import('../../src/polling.js');
    const l = log();
    await drainInFlight({ size: () => 0 }, l, 1000, 1);
    expect(l.info).not.toHaveBeenCalled();
  });

  it('waits until in-flight handlers finish', async () => {
    const { drainInFlight } = await import('../../src/polling.js');
    let pending = 3;
    const l = log();
    await drainInFlight({ size: () => (pending > 0 ? pending-- : 0) }, l, 5000, 1);
    expect(pending).toBe(0);
    expect(l.warn).not.toHaveBeenCalled();
    expect(l.info).toHaveBeenLastCalledWith({}, 'All in-flight updates finished');
  });

  it('gives up after the timeout instead of blocking shutdown forever', async () => {
    const { drainInFlight } = await import('../../src/polling.js');
    const l = log();
    await drainInFlight({ size: () => 1 }, l, 20, 5);
    expect(l.warn).toHaveBeenCalledWith(
      { pending: 1, timeoutMs: 20 },
      'Drain timed out — abandoning in-flight updates'
    );
  });

  it('stops the runner BEFORE draining (runner.stop alone does not drain)', async () => {
    const { stopLongPolling } = await import('../../src/polling.js');
    const order: string[] = [];
    let pending = 1;
    const runner = {
      isRunning: () => true,
      stop: vi.fn(async () => {
        order.push('stop');
      }),
      size: () => {
        order.push('size');
        return pending-- > 0 ? 1 : 0;
      },
    };
    await stopLongPolling(runner as never, log());
    expect(order[0]).toBe('stop');
    expect(runner.stop).toHaveBeenCalledOnce();
  });
});

describe('bot-state', () => {
  it('markBotDisconnected flips readiness off (health is not a one-way latch)', async () => {
    const state = await import('../../src/services/bot-state.js');
    state.markBotReady();
    expect(state.isBotReady()).toBe(true);
    state.markBotDisconnected();
    expect(state.isBotReady()).toBe(false);
  });
});
