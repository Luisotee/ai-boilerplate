/**
 * Integration tests for whatsapp-cloud webhook routes.
 *
 * Tests the GET /webhook (Meta verification) and POST /webhook (message
 * ingestion) routes via Fastify's inject() method.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

// ---------------------------------------------------------------------------
// Mocks — declared before imports so hoisting applies correctly.
// ---------------------------------------------------------------------------

// Mock config with a known verify token and no app secret (skip HMAC in most tests)
vi.mock('../../src/config.js', () => ({
  config: {
    meta: {
      webhookVerifyToken: 'test-verify-token',
      appSecret: '', // empty = skip HMAC verification
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

// Mock logger to silence output and prevent pino-pretty initialization
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

// Mock the message handlers so webhook POST processing does not hit real services
vi.mock('../../src/handlers/text.js', () => ({
  handleTextMessage: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../../src/handlers/audio.js', () => ({
  extractAndTranscribeAudio: vi.fn().mockResolvedValue('transcribed text'),
}));

vi.mock('../../src/handlers/image.js', () => ({
  extractImageData: vi.fn().mockResolvedValue('base64-image-data'),
}));

vi.mock('../../src/handlers/document.js', () => ({
  extractDocumentData: vi.fn().mockResolvedValue({ base64: 'pdf-data', filename: 'test.pdf' }),
}));

// Mock graph-api service (sendReaction, sendText used in error handling)
vi.mock('../../src/services/graph-api.js', () => ({
  sendText: vi.fn().mockResolvedValue('wamid.sent'),
  sendReaction: vi.fn().mockResolvedValue(undefined),
  sendImage: vi.fn().mockResolvedValue('wamid.sent_image'),
  sendDocument: vi.fn().mockResolvedValue('wamid.sent_doc'),
  sendAudio: vi.fn().mockResolvedValue('wamid.sent_audio'),
  sendVideo: vi.fn().mockResolvedValue('wamid.sent_video'),
  sendLocation: vi.fn().mockResolvedValue('wamid.sent_loc'),
  sendContact: vi.fn().mockResolvedValue('wamid.sent_contact'),
  markAsRead: vi.fn().mockResolvedValue(undefined),
  sendTypingIndicator: vi.fn().mockResolvedValue(undefined),
  uploadMedia: vi.fn().mockResolvedValue('media_id'),
  downloadMedia: vi.fn().mockResolvedValue({ buffer: Buffer.from('data'), mimetype: 'image/jpeg' }),
}));

// Mock webhook signature verification
vi.mock('../../src/utils/webhook-signature.js', () => ({
  verifyWebhookSignature: vi.fn().mockReturnValue(true),
}));

import { buildTestApp } from '../helpers/fastify.js';
import { makeWebhookBody, makeStatusWebhookBody } from '../helpers/fixtures.js';
import { handleTextMessage } from '../../src/handlers/text.js';
import { metricsRegistry } from '../../src/routes/metrics.js';

const mockHandleTextMessage = handleTextMessage as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('Webhook routes — /webhook', () => {
  let app: FastifyInstance;

  beforeAll(async () => {
    app = await buildTestApp();
  });

  afterAll(async () => {
    await app.close();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    metricsRegistry.resetMetrics();
  });

  // =========================================================================
  // GET /webhook — Meta verification handshake
  // =========================================================================
  describe('GET /webhook', () => {
    it('returns 200 with challenge when verify token matches', async () => {
      const res = await app.inject({
        method: 'GET',
        url: '/webhook',
        query: {
          'hub.mode': 'subscribe',
          'hub.verify_token': 'test-verify-token',
          'hub.challenge': '1234567890',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.body).toBe('1234567890');
    });

    it('returns 403 when verify token does not match', async () => {
      const res = await app.inject({
        method: 'GET',
        url: '/webhook',
        query: {
          'hub.mode': 'subscribe',
          'hub.verify_token': 'wrong-token',
          'hub.challenge': '1234567890',
        },
      });

      expect(res.statusCode).toBe(403);
      expect(res.json()).toEqual({ error: 'Verification failed' });
    });

    it('returns 403 when hub.mode is not subscribe', async () => {
      const res = await app.inject({
        method: 'GET',
        url: '/webhook',
        query: {
          'hub.mode': 'unsubscribe',
          'hub.verify_token': 'test-verify-token',
          'hub.challenge': '1234567890',
        },
      });

      expect(res.statusCode).toBe(403);
    });

    it('returns 403 when query params are missing', async () => {
      const res = await app.inject({
        method: 'GET',
        url: '/webhook',
      });

      expect(res.statusCode).toBe(403);
    });

    it('returns 200 with empty string when challenge is missing but token is valid', async () => {
      const res = await app.inject({
        method: 'GET',
        url: '/webhook',
        query: {
          'hub.mode': 'subscribe',
          'hub.verify_token': 'test-verify-token',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.body).toBe('');
    });
  });

  // =========================================================================
  // POST /webhook — Message ingestion
  // =========================================================================
  describe('POST /webhook', () => {
    it('returns 200 EVENT_RECEIVED for a valid text message payload', async () => {
      const payload = makeWebhookBody('16505551234', 'Hello from test');

      const res = await app.inject({
        method: 'POST',
        url: '/webhook',
        payload,
      });

      expect(res.statusCode).toBe(200);
      expect(res.body).toContain('EVENT_RECEIVED');
    });

    it('returns 200 EVENT_RECEIVED for a status update (no messages)', async () => {
      const payload = makeStatusWebhookBody('16505551234', 'delivered');

      const res = await app.inject({
        method: 'POST',
        url: '/webhook',
        payload,
      });

      expect(res.statusCode).toBe(200);
      expect(res.body).toContain('EVENT_RECEIVED');
    });

    it('returns 200 EVENT_RECEIVED for malformed payload (graceful handling)', async () => {
      // Meta expects 200 even for malformed payloads, otherwise it retries
      const res = await app.inject({
        method: 'POST',
        url: '/webhook',
        payload: { object: 'something_else' },
      });

      expect(res.statusCode).toBe(200);
    });

    it('fires processWebhookMessages for a text message and calls handleTextMessage', async () => {
      const payload = makeWebhookBody('16505551234', 'Process this');

      const res = await app.inject({
        method: 'POST',
        url: '/webhook',
        payload,
      });

      expect(res.statusCode).toBe(200);

      // processWebhookMessages runs async after the response. Give it a tick to execute.
      await new Promise((resolve) => setTimeout(resolve, 50));

      expect(mockHandleTextMessage).toHaveBeenCalledWith(
        '16505551234',
        expect.any(String),
        'Process this',
        'Test User'
      );
    });

    it('does not call handlers for status-only payloads', async () => {
      const payload = makeStatusWebhookBody('16505551234', 'read');

      await app.inject({
        method: 'POST',
        url: '/webhook',
        payload,
      });

      await new Promise((resolve) => setTimeout(resolve, 50));

      expect(mockHandleTextMessage).not.toHaveBeenCalled();
    });

    it('increments chat_messages_received_total with the right labels', async () => {
      const payload = makeWebhookBody('16505551234', 'Count me');

      await app.inject({
        method: 'POST',
        url: '/webhook',
        payload,
      });

      // processWebhookMessages is fire-and-forget; give it a tick.
      await new Promise((resolve) => setTimeout(resolve, 50));

      const exposition = await metricsRegistry.metrics();
      expect(exposition).toContain(
        'chat_messages_received_total{client="cloud",type="text",conversation_type="private"} 1'
      );
    });

    it('buckets unknown message.type values under type="other"', async () => {
      // Construct a webhook payload with an unknown message type (e.g. "interactive")
      const payload = {
        object: 'whatsapp_business_account',
        entry: [
          {
            id: 'BIZ_ID',
            changes: [
              {
                value: {
                  messaging_product: 'whatsapp',
                  metadata: { display_phone_number: '15555550000', phone_number_id: 'PNID' },
                  contacts: [{ profile: { name: 'T' }, wa_id: '16505551234' }],
                  messages: [
                    {
                      from: '16505551234',
                      id: 'wamid.interactive.1',
                      timestamp: '1700000000',
                      type: 'interactive',
                      interactive: { type: 'button_reply', button_reply: { id: 'x', title: 'y' } },
                    },
                  ],
                },
                field: 'messages',
              },
            ],
          },
        ],
      };

      await app.inject({ method: 'POST', url: '/webhook', payload });
      await new Promise((resolve) => setTimeout(resolve, 50));

      const exposition = await metricsRegistry.metrics();
      expect(exposition).toContain(
        'chat_messages_received_total{client="cloud",type="other",conversation_type="private"} 1'
      );
      expect(exposition).not.toContain('type="interactive"');
    });
  });
});
