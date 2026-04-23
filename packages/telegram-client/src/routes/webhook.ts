import type { FastifyInstance } from 'fastify';
import { webhookCallback } from 'grammy';
import { bot } from '../bot.js';
import { config } from '../config.js';

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
 */
export async function registerWebhookRoutes(app: FastifyInstance) {
  const handler = webhookCallback(bot, 'fastify', {
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
    handler
  );
}
