import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBaileysReady } from '../services/baileys.js';
import { HealthResponseSchema } from '../schemas/messaging.js';

export async function registerHealthRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().get(
    '/health',
    {
      schema: {
        tags: ['Health'],
        description: 'Health check endpoint',
        response: {
          200: HealthResponseSchema,
        },
      },
    },
    async () => {
      return {
        status: 'healthy',
        whatsapp_connected: isBaileysReady(),
      };
    }
  );
}
