import type { FastifyInstance } from 'fastify';
import client from 'prom-client';

export const metricsRegistry = new client.Registry();
client.collectDefaultMetrics({ register: metricsRegistry });

export const messagesReceived = new client.Counter({
  name: 'telegram_messages_received_total',
  help: 'Total Telegram messages received',
  labelNames: ['type', 'conversation_type'] as const,
  registers: [metricsRegistry],
});

export const messagesSent = new client.Counter({
  name: 'telegram_messages_sent_total',
  help: 'Total Telegram messages sent',
  labelNames: ['type'] as const,
  registers: [metricsRegistry],
});

export const apiPollDuration = new client.Histogram({
  name: 'ai_api_poll_duration_seconds',
  help: 'Time spent polling AI API for job results',
  labelNames: ['status'] as const,
  buckets: [0.5, 1, 2, 5, 10, 30, 60, 120],
  registers: [metricsRegistry],
});

export async function registerMetricsRoutes(app: FastifyInstance) {
  app.get('/metrics', { schema: { hide: true } }, async (_req, reply) => {
    reply.header('Content-Type', metricsRegistry.contentType).send(await metricsRegistry.metrics());
  });
}
