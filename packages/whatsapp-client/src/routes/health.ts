import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isBaileysReady } from '../services/baileys.js';
import { HealthResponseSchema, ReadyResponseSchema } from '../schemas/messaging.js';

export async function registerHealthRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().get(
    '/health',
    {
      schema: {
        tags: ['Health'],
        description:
          'Liveness probe. Returns 200 as long as the HTTP server is up; does not probe WhatsApp connectivity.',
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

  app.withTypeProvider<ZodTypeProvider>().get(
    '/health/ready',
    {
      schema: {
        tags: ['Health'],
        description:
          'Readiness probe. Returns 503 when the Baileys WhatsApp socket is not connected. Intended for Uptime Kuma / Kubernetes readiness probes.',
        response: {
          200: ReadyResponseSchema,
          503: ReadyResponseSchema,
        },
      },
    },
    async (_request, reply) => {
      const connected = isBaileysReady();
      const body = {
        status: connected ? ('ready' as const) : ('not_ready' as const),
        checks: {
          whatsapp: connected ? 'ok' : 'fail: baileys socket not connected',
        },
      };
      return reply.code(connected ? 200 : 503).send(body);
    }
  );
}
