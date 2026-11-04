/**
 * Integration tests for the whatsapp-cloud connection route.
 *
 * GET /whatsapp/status returns connection status and the business phone number.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

vi.mock('../../src/services/cloud-state.js', () => ({
  isCloudApiConnected: vi.fn(),
  setCloudApiReady: vi.fn(),
}));

vi.mock('../../src/services/graph-api.js', () => ({
  getDisplayPhoneNumber: vi.fn(),
  setDisplayPhoneNumber: vi.fn(),
  sendText: vi.fn(),
  sendReaction: vi.fn(),
  sendLocation: vi.fn(),
  sendContact: vi.fn(),
  sendImage: vi.fn(),
  sendDocument: vi.fn(),
  sendAudio: vi.fn(),
  sendVideo: vi.fn(),
  markAsRead: vi.fn(),
  sendTypingIndicator: vi.fn(),
  uploadMedia: vi.fn(),
  downloadMedia: vi.fn(),
}));

import { buildTestApp } from '../helpers/fastify.js';
import { isCloudApiConnected } from '../../src/services/cloud-state.js';
import { getDisplayPhoneNumber } from '../../src/services/graph-api.js';

const mockIsCloudApiConnected = isCloudApiConnected as ReturnType<typeof vi.fn>;
const mockGetDisplayPhoneNumber = getDisplayPhoneNumber as ReturnType<typeof vi.fn>;

describe('Connection routes (whatsapp-cloud)', () => {
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

  describe('GET /whatsapp/status', () => {
    it('returns connected status and phone number when ready', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);
      mockGetDisplayPhoneNumber.mockResolvedValue('5561999999999');

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'connected',
        phoneNumber: '5561999999999',
      });
    });

    it('returns disconnected status without calling graph API when cloud API is not ready', async () => {
      mockIsCloudApiConnected.mockReturnValue(false);
      mockGetDisplayPhoneNumber.mockResolvedValue(null);

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'disconnected',
        phoneNumber: null,
      });
      expect(mockGetDisplayPhoneNumber).not.toHaveBeenCalled();
    });

    it('returns null phone number if graph API fetch produced no number', async () => {
      mockIsCloudApiConnected.mockReturnValue(true);
      mockGetDisplayPhoneNumber.mockResolvedValue(null);

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'connected',
        phoneNumber: null,
      });
    });
  });
});
