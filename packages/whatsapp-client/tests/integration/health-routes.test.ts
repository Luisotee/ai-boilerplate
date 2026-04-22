/**
 * Integration tests for whatsapp-client health and readiness routes.
 *
 * /health is a simple liveness probe that always returns 200 as long as
 * the HTTP server is up. /health/ready probes Baileys connectivity and
 * returns 503 when the socket is not connected, so Uptime Kuma alerts fire.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

vi.mock('../../src/services/baileys.js', () => ({
  getBaileysSocket: vi.fn(),
  isBaileysReady: vi.fn(),
  setBaileysSocket: vi.fn(),
}));

import { buildTestApp } from '../helpers/fastify.js';
import { isBaileysReady } from '../../src/services/baileys.js';

const mockIsBaileysReady = isBaileysReady as ReturnType<typeof vi.fn>;

describe('Health routes', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('GET /health', () => {
    it('returns 200 with healthy status regardless of Baileys state', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({ method: 'GET', url: '/health' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status: 'healthy', whatsapp_connected: false });
    });

    it('reports whatsapp_connected=true when Baileys is ready', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({ method: 'GET', url: '/health' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status: 'healthy', whatsapp_connected: true });
    });
  });

  describe('GET /health/ready', () => {
    it('returns 200 with status=ready when Baileys is connected', async () => {
      mockIsBaileysReady.mockReturnValue(true);

      const res = await app.inject({ method: 'GET', url: '/health/ready' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'ready',
        checks: { whatsapp: 'ok' },
      });
    });

    it('returns 503 with status=not_ready when Baileys is not connected', async () => {
      mockIsBaileysReady.mockReturnValue(false);

      const res = await app.inject({ method: 'GET', url: '/health/ready' });

      expect(res.statusCode).toBe(503);
      const body = res.json();
      expect(body.status).toBe('not_ready');
      expect(body.checks.whatsapp).toMatch(/^fail:/);
    });
  });
});
