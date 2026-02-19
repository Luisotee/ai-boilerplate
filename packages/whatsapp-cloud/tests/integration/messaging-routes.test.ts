/**
 * Integration tests for whatsapp-cloud messaging routes.
 *
 * Tests the HTTP routes via Fastify's inject() method with mocked
 * Cloud API connection state, verifying correct status codes and
 * response bodies for all messaging endpoints.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

// ---------------------------------------------------------------------------
// Mocks — must be declared before any import that transitively loads the
// mocked modules.
// ---------------------------------------------------------------------------

// Mock the Cloud API state service
vi.mock('../../src/services/cloud-state.js', () => ({
  isCloudApiConnected: vi.fn(),
  setCloudApiReady: vi.fn(),
}));

// Mock the Graph API service
vi.mock('../../src/services/graph-api.js', () => ({
  sendText: vi.fn().mockResolvedValue('wamid.sent_text_123'),
  sendReaction: vi.fn().mockResolvedValue(undefined),
  sendLocation: vi.fn().mockResolvedValue('wamid.sent_location_123'),
  sendContact: vi.fn().mockResolvedValue('wamid.sent_contact_123'),
  sendImage: vi.fn().mockResolvedValue('wamid.sent_image_123'),
  sendDocument: vi.fn().mockResolvedValue('wamid.sent_document_123'),
  sendAudio: vi.fn().mockResolvedValue('wamid.sent_audio_123'),
  sendVideo: vi.fn().mockResolvedValue('wamid.sent_video_123'),
  markAsRead: vi.fn().mockResolvedValue(undefined),
  sendTypingIndicator: vi.fn().mockResolvedValue(undefined),
  uploadMedia: vi.fn().mockResolvedValue('media_upload_id_123'),
  downloadMedia: vi.fn().mockResolvedValue({
    buffer: Buffer.from('fake'),
    mimetype: 'application/octet-stream',
  }),
}));

// Mock config to prevent .env loading
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

// Mock logger
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

// Mock handlers (needed because buildTestApp registers webhook routes too)
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

import { buildTestApp } from '../helpers/fastify.js';
import { isCloudApiConnected } from '../../src/services/cloud-state.js';
import * as graphApi from '../../src/services/graph-api.js';

const mockIsCloudApiConnected = isCloudApiConnected as ReturnType<typeof vi.fn>;
const mockSendText = graphApi.sendText as ReturnType<typeof vi.fn>;
const mockSendReaction = graphApi.sendReaction as ReturnType<typeof vi.fn>;
const mockMarkAsRead = graphApi.markAsRead as ReturnType<typeof vi.fn>;
const mockSendTypingIndicator = graphApi.sendTypingIndicator as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('Cloud API messaging routes — /whatsapp/*', () => {
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

  // =========================================================================
  // POST /whatsapp/send-text
  // =========================================================================
  describe('POST /whatsapp/send-text', () => {
    it('returns 503 when Cloud API is not connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: 'Hello' },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp Cloud API not connected' });
    });

    it('returns 400 when required fields are missing', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999' }, // missing "text"
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 400 when text is empty', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: '' },
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 with message_id on success', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: 'Hello Cloud' },
      });

      expect(res.statusCode).toBe(200);
      const body = res.json();
      expect(body.success).toBe(true);
      expect(body.message_id).toBe('wamid.sent_text_123');
      expect(mockSendText).toHaveBeenCalledOnce();
      // The route calls jidToPhone which strips @s.whatsapp.net
      expect(mockSendText).toHaveBeenCalledWith(
        '5511999999999',
        'Hello Cloud',
        undefined
      );
    });

    it('strips JID suffix when phoneNumber is a JID', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999@s.whatsapp.net', text: 'Hey' },
      });

      expect(res.statusCode).toBe(200);
      expect(mockSendText).toHaveBeenCalledWith('5511999999999', 'Hey', undefined);
    });

    it('passes quoted_message_id context when provided', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: {
          phoneNumber: '5511999999999',
          text: 'Reply text',
          quoted_message_id: 'wamid.original_123',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(mockSendText).toHaveBeenCalledWith(
        '5511999999999',
        'Reply text',
        { message_id: 'wamid.original_123' }
      );
    });

    it('returns 500 when graphApi.sendText throws', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);
      mockSendText.mockRejectedValueOnce(new Error('Graph API error'));

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-text',
        payload: { phoneNumber: '5511999999999', text: 'Hello' },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to send message' });
    });
  });

  // =========================================================================
  // POST /whatsapp/send-reaction
  // =========================================================================
  describe('POST /whatsapp/send-reaction', () => {
    it('returns 503 when Cloud API is not connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_123',
          emoji: '\uD83D\uDC4D',
        },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp Cloud API not connected' });
    });

    it('returns 400 when required fields are missing', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: { phoneNumber: '5511999999999' }, // missing message_id, emoji
      });

      expect(res.statusCode).toBe(400);
    });

    it('returns 200 on success and calls graphApi.sendReaction', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: {
          phoneNumber: '5511999999999',
          message_id: 'MSG_123',
          emoji: '\uD83D\uDC4D',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSendReaction).toHaveBeenCalledOnce();
      expect(mockSendReaction).toHaveBeenCalledWith(
        '5511999999999',
        'MSG_123',
        '\uD83D\uDC4D'
      );
    });

    it('returns 500 when graphApi.sendReaction throws', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);
      mockSendReaction.mockRejectedValueOnce(new Error('API error'));

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/send-reaction',
        payload: {
          phoneNumber: '5511999999999@s.whatsapp.net',
          message_id: 'MSG_123',
          emoji: '\uD83D\uDC4D',
        },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to send reaction' });
    });
  });

  // =========================================================================
  // POST /whatsapp/typing
  // =========================================================================
  describe('POST /whatsapp/typing', () => {
    it('returns 503 when Cloud API is not connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'composing' },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp Cloud API not connected' });
    });

    it('returns 400 when composing without message_id', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'composing' },
      });

      expect(res.statusCode).toBe(400);
      expect(res.json()).toEqual({ error: 'message_id is required for composing state' });
    });

    it('returns 200 for composing state with message_id', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: {
          phoneNumber: '5511999999999',
          state: 'composing',
          message_id: 'wamid.msg_001',
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSendTypingIndicator).toHaveBeenCalledWith('wamid.msg_001');
    });

    it('returns 200 for paused state (no-op, no API call)', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'paused' },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockSendTypingIndicator).not.toHaveBeenCalled();
    });

    it('returns 400 with invalid typing state', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/typing',
        payload: { phoneNumber: '5511999999999', state: 'typing' },
      });

      expect(res.statusCode).toBe(400);
    });
  });

  // =========================================================================
  // POST /whatsapp/read-messages
  // =========================================================================
  describe('POST /whatsapp/read-messages', () => {
    it('returns 503 when Cloud API is not connected', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/read-messages',
        payload: {
          phoneNumber: '5511999999999',
          message_ids: ['MSG_001'],
        },
      });

      expect(res.statusCode).toBe(503);
      expect(res.json()).toEqual({ error: 'WhatsApp Cloud API not connected' });
    });

    it('returns 200 on success and calls markAsRead for each message', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/read-messages',
        payload: {
          phoneNumber: '5511999999999',
          message_ids: ['MSG_001', 'MSG_002', 'MSG_003'],
        },
      });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockMarkAsRead).toHaveBeenCalledTimes(3);
      expect(mockMarkAsRead).toHaveBeenNthCalledWith(1, 'MSG_001');
      expect(mockMarkAsRead).toHaveBeenNthCalledWith(2, 'MSG_002');
      expect(mockMarkAsRead).toHaveBeenNthCalledWith(3, 'MSG_003');
    });

    it('returns 500 when markAsRead throws', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);
      mockMarkAsRead.mockRejectedValueOnce(new Error('Mark read failed'));

      const res = await app.inject({
        method: 'POST',
        url: '/whatsapp/read-messages',
        payload: {
          phoneNumber: '5511999999999',
          message_ids: ['MSG_001'],
        },
      });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to mark messages as read' });
    });
  });
});
