import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildTestApp } from '../helpers/fastify.js';
import { metricsRegistry, messagesReceived } from '../../src/routes/metrics.js';

describe('GET /metrics', () => {
  let app: FastifyInstance;

  beforeEach(async () => {
    metricsRegistry.resetMetrics();
    app = await buildTestApp();
  });

  afterEach(async () => {
    await app.close();
  });

  it('returns 200 with Prometheus exposition format', async () => {
    const res = await app.inject({ method: 'GET', url: '/metrics' });

    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toMatch(/text\/plain/);
    expect(res.headers['content-type']).toContain('version=0.0.4');
    expect(res.body).toContain('# HELP whatsapp_messages_received_total');
    expect(res.body).toContain('# HELP whatsapp_messages_sent_total');
    expect(res.body).toContain('# HELP ai_api_poll_duration_seconds');
  });

  it('reflects counter increments in the exposition output', async () => {
    messagesReceived.inc({ type: 'text', conversation_type: 'private' }, 4);

    const res = await app.inject({ method: 'GET', url: '/metrics' });

    expect(res.body).toContain(
      'whatsapp_messages_received_total{type="text",conversation_type="private"} 4'
    );
  });
});
