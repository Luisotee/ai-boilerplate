import type { FastifyInstance } from 'fastify';
import type { ZodTypeProvider } from 'fastify-type-provider-zod';
import {
  getBaileysSocket,
  getConnectionInfo,
  getTimelockCache,
  isBaileysReady,
  setTimelockCache,
} from '../services/baileys.js';
import {
  ConnectionInfoSchema,
  StatusResponseSchema,
  SuccessResponseSchema,
  ErrorResponseSchema,
  ReachoutTimelockResponseSchema,
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

  app.withTypeProvider<ZodTypeProvider>().get(
    '/whatsapp/status',
    {
      schema: {
        tags: ['Connection'],
        description:
          'Current WhatsApp link status and paired phone number digits (survives reconnects). ' +
          'Requires the API key.',
        response: {
          200: StatusResponseSchema,
        },
      },
    },
    async () => {
      const info = getConnectionInfo();
      return {
        status: info.status,
        phoneNumber: info.phoneNumber,
      };
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

  app.withTypeProvider<ZodTypeProvider>().get(
    '/whatsapp/reachout-timelock',
    {
      schema: {
        tags: ['Connection'],
        description:
          'Whether WhatsApp currently restricts this account from messaging new contacts ' +
          '(reach-out timelock), and until when. The broadcast sender waits while it is ' +
          'active. Cached for 5 minutes. Requires the API key.',
        response: {
          200: ReachoutTimelockResponseSchema,
          500: ErrorResponseSchema,
          503: ErrorResponseSchema,
        },
      },
    },
    async (_request, reply) => {
      if (!isBaileysReady()) {
        return reply.code(503).send({ error: 'WhatsApp not connected' });
      }
      const sock = getBaileysSocket();
      const cached = getTimelockCache(sock);
      if (cached) return cached;
      try {
        const result = await sock.fetchAccountReachoutTimelock();
        const value = {
          active: Boolean(result.isActive),
          ends_at: result.timeEnforcementEnds?.toISOString() ?? null,
          enforcement_type: result.enforcementType ? String(result.enforcementType) : null,
        };
        setTimelockCache(sock, value);
        return value;
      } catch (err) {
        app.log.warn({ err }, 'Failed to fetch the reach-out timelock');
        return reply.code(500).send({ error: 'Failed to fetch the reach-out timelock' });
      }
    }
  );
}
