/**
 * Integration tests for whatsapp-cloud health and readiness routes.
 *
 * /health is a simple liveness probe that always returns 200 as long as
 * the HTTP server is up. /health/ready probes whether Cloud API
 * credentials validated at startup, and returns 503 when they did not.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

vi.mock('../../src/services/cloud-state.js', () => ({
  isCloudApiConnected: vi.fn(),
  setCloudApiReady: vi.fn(),
}));

// Handlers are loaded transitively via the webhook route registration;
// stub them to avoid pulling real config / network code.
vi.mock('../../src/handlers/text.js', () => ({
  handleTextMessage: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('../../src/handlers/audio.js', () => ({
  extractAndTranscribeAudio: vi.fn().mockResolvedValue('transcribed'),
}));
vi.mock('../../src/handlers/image.js', () => ({
  extractImageData: vi.fn().mockResolvedValue('base64-data'),
}));
vi.mock('../../src/handlers/document.js', () => ({
  extractDocumentData: vi.fn().mockResolvedValue({ base64: 'pdf', filename: 'f.pdf' }),
}));
vi.mock('../../src/utils/webhook-signature.js', () => ({
  verifyWebhookSignature: vi.fn().mockReturnValue(true),
}));
vi.mock('../../src/config.js', () => ({
  config: {
    meta: {
      webhookVerifyToken: 'test-verify-token',
      appSecret: '',
      phoneNumberId: 'PHONE_NUMBER_ID',
      accessToken: 'TEST_ACCESS_TOKEN',
      graphApiVersion: 'v21.0',
      graphApiBaseUrl: 'https://graph.facebook.com',
    },
    whitelistPhones: new Set<string>(),
    aiApiUrl: 'http://localhost:8000',
    aiApiKey: 'test-key',
    logLevel: 'silent',
    server: { port: 3002, host: '0.0.0.0' },
    whatsappApiKey: 'test-key',
    corsOrigins: '',
    rateLimitGlobal: 30,
    rateLimitExpensive: 5,
    timeouts: { default: 30000, transcription: 60000, tts: 45000, polling: 5000 },
    polling: { intervalMs: 500, maxIterations: 240, maxDurationMs: 120000 },
  },
}));
vi.mock('../../src/logger.js', () => ({
  logger: {
    info: vi.fn(),
    error: vi.fn(),
    warn: vi.fn(),
    debug: vi.fn(),
    trace: vi.fn(),
    fatal: vi.fn(),
    child: vi.fn().mockReturnValue({
      info: vi.fn(),
      error: vi.fn(),
      warn: vi.fn(),
      debug: vi.fn(),
    }),
  },
}));

import { buildTestApp } from '../helpers/fastify.js';
import { isCloudApiConnected } from '../../src/services/cloud-state.js';

const mockIsCloudApiConnected = isCloudApiConnected as ReturnType<typeof vi.fn>;

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
    it('returns 200 with status=disconnected when Cloud API credentials unverified', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);

      const res = await app.inject({ method: 'GET', url: '/health' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status: 'disconnected', whatsapp_connected: false });
    });

    it('returns 200 with status=healthy when Cloud API is connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({ method: 'GET', url: '/health' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status: 'healthy', whatsapp_connected: true });
    });
  });

  describe('GET /health/ready', () => {
    it('returns 200 with status=ready when Cloud API is connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({ method: 'GET', url: '/health/ready' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'ready',
        checks: { whatsapp: 'ok' },
      });
    });

    it('returns 503 with status=not_ready when Cloud API is not connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);

      const res = await app.inject({ method: 'GET', url: '/health/ready' });

      expect(res.statusCode).toBe(503);
      const body = res.json();
      expect(body.status).toBe('not_ready');
      expect(body.checks.whatsapp).toMatch(/^fail:/);
    });
  });
});
