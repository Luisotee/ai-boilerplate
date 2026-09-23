import { autoChatAction, type AutoChatActionFlavor } from '@grammyjs/auto-chat-action';
import { autoRetry } from '@grammyjs/auto-retry';
import { sequentialize } from '@grammyjs/runner';
import { Bot, GrammyError, HttpError, type Context } from 'grammy';
import { config } from './config.js';
import { Sentry } from './instrument.js';
import { logger } from './logger.js';

export type TelegramContext = Context & AutoChatActionFlavor;

export const bot = new Bot<TelegramContext>(config.telegram.botToken);

const polling = config.telegram.mode === 'polling';

// Plugins:
//  - auto-retry: handles 429 + 5xx with exponential backoff.
//
//    Polling mode needs `rethrowHttpErrors: true`, and it is load-bearing.
//    auto-retry installs on `bot.api`, UNDERNEATH the runner's
//    `bot.api.getUpdates` call, and its HttpError branch retries in an inner
//    loop that never decrements `maxRetryAttempts`. Left off, a network
//    partition is swallowed there forever: the runner never sees a failure, its
//    `maxRetryTime` never applies, `runner.task()` never rejects, and main.ts's
//    exit-and-restart path never fires — the bot goes deaf while /health still
//    reports OK. The trade-off: a *send* hitting a network error now fails fast
//    into the handler's error path instead of retrying silently. Webhook mode
//    keeps the historical options (no long-running poll loop to protect).
//  - sequentialize (polling only): the runner processes updates CONCURRENTLY —
//    the point of it, since one AI reply can take up to POLL_MAX_DURATION_MS —
//    so this restores per-chat ordering. Registered BEFORE auto-chat-action so
//    the typing indicator belongs to the update actually being handled.
//  - auto-chat-action: keeps sendChatAction refreshing across long handlers.
// Downloads go through services/telegram-api.ts (bot.api.getFile + fetch).
bot.api.config.use(
  autoRetry({
    maxRetryAttempts: 3,
    maxDelaySeconds: 30,
    ...(polling && { rethrowHttpErrors: true }),
  })
);
if (polling) {
  bot.use(sequentialize((ctx) => ctx.chat?.id.toString()));
}
bot.use(autoChatAction());

/** Structured logging + Sentry for anything a handler throws (polling mode). */
export function reportBotError(err: unknown, ctx?: TelegramContext): void {
  const meta = {
    updateId: ctx?.update.update_id,
    chatId: ctx?.chatId,
    fromId: ctx?.from?.id,
  };
  if (err instanceof GrammyError) {
    logger.error(
      { ...meta, description: err.description, method: err.method, errorCode: err.error_code },
      'Telegram API rejected a call'
    );
  } else if (err instanceof HttpError) {
    logger.error({ ...meta, err }, 'Could not reach Telegram');
  } else {
    logger.error({ ...meta, err }, 'Unhandled error while processing update');
  }
  Sentry.captureException(err);
}

if (polling) {
  // Two layers, deliberately:
  //
  // 1. An outermost middleware boundary. It must swallow — a rethrow would let
  //    the runner treat the bot as crashed.
  // 2. bot.catch, which the runner's sink invokes via `bot.errorHandler`: the
  //    backstop for anything thrown outside the middleware stack. It must never
  //    throw, or the runner prints a "your error handler threw" banner and the
  //    update is lost.
  bot.use(async (ctx, next) => {
    try {
      await next();
    } catch (err) {
      reportBotError(err, ctx);
    }
  });

  bot.catch((err) => {
    reportBotError(err.error, err.ctx as TelegramContext);
  });
}

// Webhook mode: bot.catch() intentionally not registered. grammY's
// `webhookCallback` calls `bot.handleUpdate()` (singular), which re-throws
// `BotError` without invoking the error handler — `bot.catch` only fires for
// the long-polling path. Structured error logging lives in routes/webhook.ts,
// and the rethrow makes Fastify answer 500 so Telegram retries the delivery.
