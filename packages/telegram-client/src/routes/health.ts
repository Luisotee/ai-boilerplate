import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBotReady } from '../services/bot-state.js';
import { HealthResponseSchema, ReadyResponseSchema } from '../schemas/messaging.js';

export async function registerHealthRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().get(
    '/health',
    {
      schema: {
        tags: ['Health'],
        description:
          'Liveness probe. Returns 200 as long as the HTTP server is up; does not probe Bot API connectivity.',
        response: { 200: HealthResponseSchema },
      },
    },
    async () => ({
      status: isBotReady() ? 'healthy' : 'disconnected',
      telegram_connected: isBotReady(),
    })
  );

  app.withTypeProvider<ZodTypeProvider>().get(
    '/health/ready',
    {
      schema: {
        tags: ['Health'],
        description: 'Readiness probe. 503 while bot.init() has not successfully fetched getMe.',
        response: { 200: ReadyResponseSchema, 503: ReadyResponseSchema },
      },
    },
    async (_req, reply) => {
      const ready = isBotReady();
      const body = {
        status: ready ? ('ready' as const) : ('not_ready' as const),
        checks: { telegram: ready ? 'ok' : 'fail: getMe not yet successful' },
      };
      return reply.code(ready ? 200 : 503).send(body);
    }
  );
}
