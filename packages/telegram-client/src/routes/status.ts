import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBotReady, getBotUsername } from '../services/bot-state.js';
import { StatusResponseSchema } from '../schemas/messaging.js';

export async function registerStatusRoutes(app: FastifyInstance) {
  // GET /whatsapp/status
  // Uses the /whatsapp/* namespace to conform to repo convention for inter-service status.
  app.withTypeProvider<ZodTypeProvider>().get(
    '/whatsapp/status',
    {
      schema: {
        tags: ['Status'],
        description:
          'Current Telegram bot connection status and bot username. Requires the API key.',
        response: {
          200: StatusResponseSchema,
        },
      },
    },
    async () => {
      const ready = isBotReady();
      const username = getBotUsername();
      return {
        status: ready ? ('connected' as const) : ('disconnected' as const),
        username,
      };
    }
  );
}
