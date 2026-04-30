import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { FastifyInstance } from 'fastify';
import { buildTestApp } from '../helpers/fastify.js';
import { metricsRegistry, messagesReceived } from '../../src/routes/metrics.js';

describe('GET /metrics (cloud)', () => {
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
    expect(res.body).toContain('# HELP chat_messages_received_total');
  });

  it('reflects counter increments in the exposition output', async () => {
    messagesReceived.inc({ client: 'cloud', type: 'image', conversation_type: 'private' }, 2);

    const res = await app.inject({ method: 'GET', url: '/metrics' });

    expect(res.body).toContain(
      'chat_messages_received_total{client="cloud",type="image",conversation_type="private"} 2'
    );
  });
});
