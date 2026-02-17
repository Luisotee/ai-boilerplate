import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isCloudApiConnected } from '../services/cloud-state.js';
import * as graphApi from '../services/graph-api.js';
import { jidToPhone } from '../utils/jid.js';
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
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const phone = jidToPhone(request.body.phoneNumber);
        const { text, quoted_message_id } = request.body;

        const messageId = await graphApi.sendText(
          phone,
          text,
          quoted_message_id ? { message_id: quoted_message_id } : undefined
        );
        return { success: true, message_id: messageId };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send message');
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
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const phone = jidToPhone(request.body.phoneNumber);
        const { message_id, emoji } = request.body;

        await graphApi.sendReaction(phone, message_id, emoji);
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to send reaction');
        return reply.code(500).send({ error: 'Failed to send reaction' });
      }
    }
  );

  // POST /whatsapp/typing
  // Shows typing indicator via the Graph API mark-as-read endpoint.
  // 'paused' state is a no-op — Cloud API typing dismisses automatically after 25s or on response.
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/typing',
    {
      schema: {
        tags: ['Messaging'],
        description:
          'Show typing indicator. Requires message_id for composing state. Paused is a no-op (typing dismisses automatically).',
        body: TypingIndicatorSchema,
        response: {
          200: SuccessResponseSchema,
          400: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      const { state, message_id } = request.body;

      if (state === 'composing') {
        if (!message_id) {
          return reply.code(400).send({ error: 'message_id is required for composing state' });
        }
        await graphApi.sendTypingIndicator(message_id);
      }

      // 'paused' is a no-op — typing dismisses automatically
      return { success: true };
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
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isCloudApiConnected()) {
        return reply.code(503).send({ error: 'WhatsApp Cloud API not connected' });
      }

      try {
        const { message_ids } = request.body;

        // Mark each message as read
        for (const msgId of message_ids) {
          await graphApi.markAsRead(msgId);
        }
        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to mark messages as read');
        return reply.code(500).send({ error: 'Failed to mark messages as read' });
      }
    }
  );
}
