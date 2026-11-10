import type { FastifyReply } from 'fastify';
import { WhatsAppNotReadyError } from '../services/baileys.js';

/**
 * Map a send that never started because the link was down (`sendSerialized`
 * threw `WhatsAppNotReadyError`) to 503: nothing was sent, so a caller (the
 * broadcast lane) can retry it safely. Returns null for any other error.
 * Every route that sends through `sendSerialized` calls this first in its catch.
 */
export function replyIfNotConnected(err: unknown, reply: FastifyReply): FastifyReply | null {
  if (!(err instanceof WhatsAppNotReadyError)) return null;
  return reply.code(503).send({ error: 'WhatsApp not connected' });
}
