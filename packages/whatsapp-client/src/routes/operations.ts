import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { getBaileysSocket, isBaileysReady } from '../services/baileys.js';
import { normalizeJid } from '../utils/jid.js';
import {
  EditMessageSchema,
  DeleteMessageSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
} from '../schemas/messaging.js';

export async function registerOperationsRoutes(app: FastifyInstance) {
  // POST /whatsapp/edit-message
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/edit-message',
    {
      schema: {
        tags: ['Operations'],
        description: 'Edit a previously sent text message',
        body: EditMessageSchema,
        response: {
          200: SuccessResponseSchema,
          400: ErrorResponseSchema,
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
        const { message_id, new_text } = request.body;
        const sock = getBaileysSocket();

        // Create message key for the message to edit
        const messageKey = {
          remoteJid: normalizedJid,
          id: message_id,
          fromMe: true, // Can only edit messages sent by us
        };

        // Send edit message
        await sock.sendMessage(normalizedJid, {
          text: new_text,
          edit: messageKey,
        });

        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to edit message');

        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        // Handle time limit or permission errors
        if (error.message?.includes('too old') || error.message?.includes('edit')) {
          return reply.code(400).send({
            error: 'Cannot edit message (too old or not yours)',
          });
        }

        return reply.code(500).send({ error: 'Failed to edit message' });
      }
    }
  );

  // DELETE /whatsapp/delete-message
  app.withTypeProvider<ZodTypeProvider>().delete(
    '/whatsapp/delete-message',
    {
      schema: {
        tags: ['Operations'],
        description: 'Delete a message for everyone',
        body: DeleteMessageSchema,
        response: {
          200: SuccessResponseSchema,
          400: ErrorResponseSchema,
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
        const { message_id } = request.body;
        const sock = getBaileysSocket();

        // Create message key for the message to delete
        const messageKey = {
          remoteJid: normalizedJid,
          id: message_id,
          fromMe: true, // Can only delete messages sent by us
        };

        // Send delete message
        await sock.sendMessage(normalizedJid, {
          delete: messageKey,
        });

        return { success: true };
      } catch (err) {
        const error = err as Error;
        app.log.error({ error }, 'Failed to delete message');

        if (error.message?.includes('not registered on WhatsApp')) {
          return reply.code(404).send({ error: error.message });
        }

        // Handle time limit or permission errors
        if (error.message?.includes('too old') || error.message?.includes('delete')) {
          return reply.code(400).send({
            error: 'Cannot delete message (too old or not yours)',
          });
        }

        return reply.code(500).send({ error: 'Failed to delete message' });
      }
    }
  );
}
