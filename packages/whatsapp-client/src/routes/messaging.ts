import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { getBaileysSocket, isBaileysReady } from '../services/baileys.js';
import { normalizeJid } from '../utils/jid.js';
import {
  SendTextSchema,
  SendReactionSchema,
  TypingIndicatorSchema,
  ReadMessagesSchema,
  SendTextResponseSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
} from '../schemas/messaging.js';

export async function registerMessagingRoutes(app: FastifyInstance) {
  // POST /whatsapp/send-text
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-text',
    {
      schema: {
        tags: ['Messaging'],
        description: 'Send a text message to WhatsApp user or group',
        body: SendTextSchema,
        response: {
          200: SendTextResponseSchema,
          404: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const normalizedJid = await normalizeJid(request.body.phoneNumber);
        const { text } = request.body;
        const sock = getBaileysSocket();

        const result = await sock.sendMessage(normalizedJid, { text });
        return { success: true, message_id: result?.key.id };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send message');

        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        return reply.code(500).send({ error: 'Failed to send message' });
      }
    }
  );

  // POST /whatsapp/send-reaction
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/send-reaction',
    {
      schema: {
        tags: ['Messaging'],
        description: 'React to a message with emoji',
        body: SendReactionSchema,
        response: {
          200: SuccessResponseSchema,
          404: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const normalizedJid = await normalizeJid(request.body.phoneNumber);
        const { message_id, emoji } = request.body;
        const sock = getBaileysSocket();

        await sock.sendMessage(normalizedJid, {
          react: {
            text: emoji,
            key: { remoteJid: normalizedJid, id: message_id, fromMe: false },
          },
        });
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send reaction');

        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        return reply.code(500).send({ error: 'Failed to send reaction' });
      }
    }
  );

  // POST /whatsapp/typing
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/typing',
    {
      schema: {
        tags: ['Messaging'],
        description: 'Show or hide typing indicator',
        body: TypingIndicatorSchema,
        response: {
          200: SuccessResponseSchema,
          404: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const normalizedJid = await normalizeJid(request.body.phoneNumber);
        const { state } = request.body;
        const sock = getBaileysSocket();

        await sock.sendPresenceUpdate(state, normalizedJid);
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to update typing indicator');

        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        return reply.code(500).send({ error: 'Failed to update typing indicator' });
      }
    }
  );

  // POST /whatsapp/read-messages
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/read-messages',
    {
      schema: {
        tags: ['Messaging'],
        description: 'Mark messages as read',
        body: ReadMessagesSchema,
        response: {
          200: SuccessResponseSchema,
          404: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const normalizedJid = await normalizeJid(request.body.phoneNumber);
        const { message_ids } = request.body;
        const sock = getBaileysSocket();

        const keys = message_ids.map((id) => ({
          remoteJid: normalizedJid,
          id,
          fromMe: false,
        }));
        await sock.readMessages(keys);
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to mark messages as read');

        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        return reply.code(500).send({ error: 'Failed to mark messages as read' });
      }
    }
  );
}
