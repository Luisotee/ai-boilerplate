import { autoChatAction, type AutoChatActionFlavor } from '@grammyjs/auto-chat-action';
import { autoRetry } from '@grammyjs/auto-retry';
import { Bot, type Context, GrammyError, HttpError } from 'grammy';
import { config } from './config.js';
import { logger } from './logger.js';

export type TelegramContext = Context & AutoChatActionFlavor;

export const bot = new Bot<TelegramContext>(config.telegram.botToken);

// Plugins:
//  - auto-retry: handles 429 + 5xx with exponential backoff
//  - auto-chat-action: keeps sendChatAction refreshing across long handlers
// Downloads go through services/telegram-api.ts (bot.api.getFile + fetch).
bot.api.config.use(autoRetry({ maxRetryAttempts: 3, maxDelaySeconds: 30 }));
bot.use(autoChatAction());

// Structured error boundary. Webhook-mode errors also bubble to Fastify's
// error handler (and therefore Sentry), but logging the update context here
// is how we get actionable debugging information.
bot.catch((err) => {
  const cause = err.error;
  const updateId = err.ctx.update.update_id;
  const chatId = err.ctx.chat?.id;
  if (cause instanceof GrammyError) {
    logger.error(
      { err: cause, method: cause.method, description: cause.description, updateId, chatId },
      'Telegram API rejected request'
    );
  } else if (cause instanceof HttpError) {
    logger.error({ err: cause, updateId, chatId }, 'Network error reaching Telegram');
  } else {
    logger.error({ err: cause, updateId, chatId }, 'Unhandled error in bot handler');
  }
});
