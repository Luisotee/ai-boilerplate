import { autoChatAction, type AutoChatActionFlavor } from '@grammyjs/auto-chat-action';
import { autoRetry } from '@grammyjs/auto-retry';
import { Bot, type Context } from 'grammy';
import { config } from './config.js';

export type TelegramContext = Context & AutoChatActionFlavor;

export const bot = new Bot<TelegramContext>(config.telegram.botToken);

// Plugins:
//  - auto-retry: handles 429 + 5xx with exponential backoff
//  - auto-chat-action: keeps sendChatAction refreshing across long handlers
// Downloads go through services/telegram-api.ts (bot.api.getFile + fetch).
bot.api.config.use(autoRetry({ maxRetryAttempts: 3, maxDelaySeconds: 30 }));
bot.use(autoChatAction());

// NOTE: bot.catch() intentionally not registered. In webhook mode grammY's
// `webhookCallback` calls `bot.handleUpdate()` (singular), which re-throws
// `BotError` without invoking the error handler — `bot.catch` only fires for
// `bot.handleUpdates()` (the long-polling loop). Structured error logging
// lives in routes/webhook.ts where it can actually run.
