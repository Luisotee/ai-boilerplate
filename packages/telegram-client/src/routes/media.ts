import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBotReady } from '../services/bot-state.js';
import * as telegramApi from '../services/telegram-api.js';
import { resolveChatId, sendErrorResponse } from '../utils/chat-id.js';
import {
  SendLocationSchema,
  SendContactSchema,
  MediaResponseSchema,
  ErrorResponseSchema,
} from '../schemas/media.js';

/**
 * Media routes for the Telegram client.
 *
 * Schemas are field-identical to `whatsapp-client/src/schemas/media.ts` so the
 * shared Python `WhatsAppClient` works against this client unchanged.
 *
 * These back the agent's `send_whatsapp_location` / `send_whatsapp_contact`
 * tools, which previously failed with 501 inside Telegram conversations.
 */
export async function registerMediaRoutes(app: FastifyInstance) {
  // POST /whatsapp/send-location
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-location',
    {
      schema: {
        tags: ['Media'],
        description: 'Send a location pin (or a venue card when named) to a Telegram chat',
        body: SendLocationSchema,
        response: {
          200: MediaResponseSchema,
          400: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBotReady()) {
        return reply.code(503).send({ error: 'Telegram bot not ready' });
      }
      const { phoneNumber, latitude, longitude, name, address } = request.body;
      try {
        const chatId = resolveChatId(phoneNumber);
        const messageId = await telegramApi.sendLocation(
          chatId,
          latitude,
          longitude,
          name,
          address
        );
        return reply.send({ success: true, message_id: String(messageId) });
      } catch (err) {
        const error = err as Error;
        // Must log here: sendErrorResponse *returns* a reply rather than
        // throwing, so Sentry's Fastify error handler never sees it.
        app.log.error({ error }, 'Failed to send location');
        return sendErrorResponse(reply, err, 'Failed to send location');
      }
    }
  );

  // POST /whatsapp/send-contact
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-contact',
    {
      schema: {
        tags: ['Media'],
        description: 'Send a contact card to a Telegram chat',
        body: SendContactSchema,
        response: {
          200: MediaResponseSchema,
          400: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBotReady()) {
        return reply.code(503).send({ error: 'Telegram bot not ready' });
      }
      const { phoneNumber, contactName, contactPhone, contactEmail, contactOrg } = request.body;
      try {
        const chatId = resolveChatId(phoneNumber);
        const messageId = await telegramApi.sendContact(chatId, {
          name: contactName,
          phone: contactPhone,
          email: contactEmail,
          org: contactOrg,
        });
        return reply.send({ success: true, message_id: String(messageId) });
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send contact');
        return sendErrorResponse(reply, err, 'Failed to send contact');
      }
    }
  );
}
