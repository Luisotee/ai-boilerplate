import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isCloudApiConnected } from '../services/cloud-state.js';
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
        status: isCloudApiConnected() ? 'healthy' : 'disconnected',
        whatsapp_connected: isCloudApiConnected(),
      };
    }
  );
}
