import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isCloudApiConnected } from '../services/cloud-state.js';
import { getDisplayPhoneNumber } from '../services/graph-api.js';
import { ConnectionInfoSchema } from '../schemas/messaging.js';

export async function registerConnectionRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().get(
    '/whatsapp/status',
    {
      schema: {
        tags: ['Connection'],
        description:
          'Current WhatsApp Cloud API connection status and business phone number digits. ' +
          'Requires the API key.',
        response: {
          200: ConnectionInfoSchema,
        },
      },
    },
    async () => {
      const connected = isCloudApiConnected();
      const phoneNumber = connected ? await getDisplayPhoneNumber() : null;
      return {
        status: connected ? ('connected' as const) : ('disconnected' as const),
        phoneNumber,
      };
    }
  );
}
