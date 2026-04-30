import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';

/**
 * Media routes for the Telegram client.
 *
 * Telegram's first-class primitives (typing, reactions, replies, voice notes)
 * are already covered by the webhook + messaging routes. The WhatsApp-only
 * features the AI API agent exposes as tools (send_whatsapp_location,
 * send_whatsapp_contact) are stubbed as 501 — if the agent calls them inside
 * a Telegram conversation the WhatsAppClient will surface the error back to
 * the agent as a tool failure, which the agent can handle gracefully.
 */
export async function registerMediaRoutes(app: FastifyInstance) {
  const notImplemented = (feature: string) => async (_req: FastifyRequest, reply: FastifyReply) =>
    reply.code(501).send({
      error: `${feature} is not supported on the Telegram client.`,
    });

  app.post(
    '/whatsapp/send-location',
    { schema: { tags: ['Media'], description: 'Not supported on Telegram — returns 501' } },
    notImplemented('send-location')
  );

  app.post(
    '/whatsapp/send-contact',
    { schema: { tags: ['Media'], description: 'Not supported on Telegram — returns 501' } },
    notImplemented('send-contact')
  );
}
