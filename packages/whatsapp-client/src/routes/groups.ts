import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import { getBaileysSocket, isBaileysReady } from '../services/baileys.js';
import { findSharedGroups } from '../services/groups.js';
import { SharedGroupsRequestSchema, SharedGroupsResponseSchema } from '../schemas/groups.js';
import { ErrorResponseSchema } from '../schemas/messaging.js';

export async function registerGroupsRoutes(app: FastifyInstance) {
  // POST /whatsapp/shared-groups
  // POST (not GET) so the requester's identifiers stay out of query strings and
  // access logs. Membership is resolved here, server-side, on every call — the
  // AI API never trusts a group list it did not just receive from this route.
  app.withTypeProvider<ZodTypeProvider>().post(
    '/whatsapp/shared-groups',
    {
      schema: {
        tags: ['Groups'],
        description: 'List groups shared by the bot and the requesting user',
        body: SharedGroupsRequestSchema,
        response: {
          200: SharedGroupsResponseSchema,
          400: ErrorResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }

      try {
        const groups = await findSharedGroups(getBaileysSocket(), request.body);
        return { groups };
      } catch (err) {
        request.log.error({ err }, 'Failed to list shared groups');
        return reply.code(500).send({ error: 'Failed to list shared groups' });
      }
    }
  );
}
