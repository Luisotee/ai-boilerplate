import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { isCloudApiConnected } from '../services/cloud-state.js';
import { HealthResponseSchema, ReadyResponseSchema } from '../schemas/messaging.js';

export async function registerHealthRoutes(app: FastifyInstance) {
  app.withTypeProvider<ZodTypeProvider>().get(
    '/health',
    {
      schema: {
        tags: ['Health'],
        description:
          'Liveness probe. Returns 200 as long as the HTTP server is up; does not probe Cloud API connectivity.',
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

  app.withTypeProvider<ZodTypeProvider>().get(
    '/health/ready',
    {
      schema: {
        tags: ['Health'],
        description:
          'Readiness probe. Returns 503 when WhatsApp Cloud API credentials failed startup validation. Intended for Uptime Kuma / Kubernetes readiness probes.',
        response: {
          200: ReadyResponseSchema,
          503: ReadyResponseSchema,
        },
      },
    },
    async (_request, reply) => {
      const connected = isCloudApiConnected();
      const body = {
        status: connected ? ('ready' as const) : ('not_ready' as const),
        checks: {
          whatsapp: connected ? 'ok' : 'fail: cloud api credentials unverified',
        },
      };
      return reply.code(connected ? 200 : 503).send(body);
    }
  );
}
