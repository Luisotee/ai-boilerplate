import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { GrammyError, HttpError, webhookCallback } from 'grammy';
import type { Update } from 'grammy/types';
import { bot } from '../bot.js';
import { config } from '../config.js';
import { logger } from '../logger.js';

/**
 * POST /webhook — Telegram update delivery.
 *
 * grammY's webhookCallback adapter:
 *   - handles JSON decoding of the Update
 *   - dispatches to registered bot.on(...) handlers
 *   - validates the `X-Telegram-Bot-Api-Secret-Token` header when secretToken
 *     is provided (rejects mismatches with 401 automatically)
 *
 * This route is intentionally registered before any API-key auth hook runs —
 * webhook deliveries are verified by Telegram's secret_token instead.
 *
 * Error handling: `bot.catch` does NOT fire in webhook mode (grammY only invokes
 * it for the long-polling `handleUpdates` path). We wrap the callback here to
 * log update context with the right structure, then re-throw so Fastify returns
 * 500 and Telegram retries. Sentry's Fastify error handler captures the throw.
 */
export async function registerWebhookRoutes(app: FastifyInstance) {
  const callback = webhookCallback(bot, 'fastify', {
    secretToken: config.telegram.webhookSecret || undefined,
  });

  app.post(
    '/webhook',
    {
      schema: {
        tags: ['Webhook'],
        description: 'Receive Telegram updates',
        hide: true,
      },
      // Skip Zod validation — grammY consumes the raw request body
      validatorCompiler: () => (data) => ({ value: data }),
      serializerCompiler: () => (data) => (typeof data === 'string' ? data : JSON.stringify(data)),
    },
    async (request: FastifyRequest, reply: FastifyReply) => {
      try {
        return await callback(request, reply);
      } catch (err) {
        logWebhookError(err, request.body);
        throw err;
      }
    }
  );
}

function logWebhookError(err: unknown, body: unknown): void {
  const { updateId, chatId } = extractUpdateContext(body);
  if (err instanceof GrammyError) {
    logger.error(
      { err, method: err.method, description: err.description, updateId, chatId },
      'Telegram API rejected request'
    );
  } else if (err instanceof HttpError) {
    logger.error({ err, updateId, chatId }, 'Network error reaching Telegram');
  } else {
    logger.error({ err, updateId, chatId }, 'Unhandled error in bot handler');
  }
}

function extractUpdateContext(body: unknown): {
  updateId: number | undefined;
  chatId: number | undefined;
} {
  if (!body || typeof body !== 'object') return { updateId: undefined, chatId: undefined };
  const update = body as Partial<Update>;
  const message =
    update.message ?? update.edited_message ?? update.channel_post ?? update.edited_channel_post;
  return {
    updateId: update.update_id,
    chatId: message?.chat?.id,
  };
}
