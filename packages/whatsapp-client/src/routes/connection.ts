import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { getConnectionInfo } from '../services/baileys.js';
import { ConnectionInfoSchema } from '../schemas/messaging.js';

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
}
