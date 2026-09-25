import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { getConnectionInfo } from '../services/baileys.js';
import {
  ConnectionInfoSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
} from '../schemas/messaging.js';
import { logoutWhatsApp } from '../whatsapp.js';

export async function registerConnectionRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().get(
    '/whatsapp/qr',
    {
      schema: {
        tags: ['Connection'],
        description:
          'Current WhatsApp link status and the latest pairing QR (while unpaired). ' +
          'Requires the API key — the QR can link a device to this account. ' +
          'Baileys rotates the QR every ~20s, so poll while pairing.',
        response: {
          200: ConnectionInfoSchema,
        },
      },
    },
    async () => {
      return getConnectionInfo();
    }
  );

  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/logout',
    {
      schema: {
        tags: ['Connection'],
        description:
          'Force-unlink the WhatsApp session and re-initialise so a fresh pairing ' +
          'QR is generated. Requires the API key. Poll GET /whatsapp/qr afterward ' +
          'for the new code.',
        response: {
          200: SuccessResponseSchema,
          500: ErrorResponseSchema,
        },
      },
    },
    async (_request, reply) => {
      try {
        await logoutWhatsApp();
        return { success: true };
      } catch (err) {
        app.log.error({ err }, 'Failed to force WhatsApp logout');
        return reply.code(500).send({ error: 'Failed to logout WhatsApp session' });
      }
    }
  );
}
