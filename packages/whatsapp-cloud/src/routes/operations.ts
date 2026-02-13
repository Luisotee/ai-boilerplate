import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import {
  EditMessageSchema,
  DeleteMessageSchema,
  ErrorResponseSchema,
} from '../schemas/messaging.js';

export async function registerOperationsRoutes(app: FastifyInstance) {
  // POST /whatsapp/edit-message
  // Cloud API does not support message editing — return 501 Not Implemented
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/edit-message',
    {
      schema: {
        tags: ['Operations'],
        description: 'Edit a previously sent text message (not supported by Cloud API)',
        body: EditMessageSchema,
        response: {
          501: ErrorResponseSchema,
        },
      },
    },
    async (_request, reply) => {
      return reply.code(501).send({
        error: 'Message editing is not supported by the WhatsApp Cloud API',
      });
    }
  );

  // DELETE /whatsapp/delete-message
  // Cloud API does not support message deletion — return 501 Not Implemented
  app.withTypeProvider<ZodTypeProvider>().delete(
    '/whatsapp/delete-message',
    {
      schema: {
        tags: ['Operations'],
        description: 'Delete a message for everyone (not supported by Cloud API)',
        body: DeleteMessageSchema,
        response: {
          501: ErrorResponseSchema,
        },
      },
    },
    async (_request, reply) => {
      return reply.code(501).send({
        error: 'Message deletion is not supported by the WhatsApp Cloud API',
      });
    }
  );
}
