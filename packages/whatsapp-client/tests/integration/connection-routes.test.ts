/**
 * Integration tests for the whatsapp-client connection route.
 *
 * GET /whatsapp/qr returns the current link status and the latest pairing QR
 * (while unpaired) so the dashboard can render and poll it.
 */

import { describe, it, expect, vi, beforeAll, beforeEach, afterAll } from 'vitest';
import type { FastifyInstance } from 'fastify';

vi.mock('../../src/services/baileys.js', () => ({
  getBaileysSocket: vi.fn(),
  isBaileysReady: vi.fn(),
  setBaileysSocket: vi.fn(),
  setConnectionStatus: vi.fn(),
  setLatestQr: vi.fn(),
  getConnectionInfo: vi.fn(),
}));

// The route imports logoutWhatsApp from whatsapp.ts, which pulls in real baileys +
// filesystem side effects. Mock it so hitting POST /whatsapp/logout doesn't run the
// real teardown (which rm's the auth_info_baileys dir).
vi.mock('../../src/whatsapp.js', () => ({
  logoutWhatsApp: vi.fn(),
  initializeWhatsApp: vi.fn(),
}));

import { buildTestApp } from '../helpers/fastify.js';
import { getConnectionInfo } from '../../src/services/baileys.js';
import { logoutWhatsApp } from '../../src/whatsapp.js';

const mockGetConnectionInfo = getConnectionInfo as ReturnType<typeof vi.fn>;
const mockLogoutWhatsApp = logoutWhatsApp as ReturnType<typeof vi.fn>;

describe('Connection routes', () => {
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

  describe('GET /whatsapp/qr', () => {
    it('returns the pairing QR and status while unpaired', async () => {
      mockGetConnectionInfo.mockReturnValue({
        status: 'qr',
        qr: '2@abc123def',
        qrGeneratedAt: '2026-05-26T14:00:00.000Z',
      });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/qr' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'qr',
        qr: '2@abc123def',
        qrGeneratedAt: '2026-05-26T14:00:00.000Z',
      });
    });

    it('reports connected with no QR once paired', async () => {
      mockGetConnectionInfo.mockReturnValue({
        status: 'connected',
        qr: null,
        qrGeneratedAt: null,
      });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/qr' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status: 'connected', qr: null, qrGeneratedAt: null });
    });

    it.each(['disconnected', 'connecting'] as const)('reports %s with no QR', async (status) => {
      mockGetConnectionInfo.mockReturnValue({ status, qr: null, qrGeneratedAt: null });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/qr' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status, qr: null, qrGeneratedAt: null });
    });
  });

  describe('POST /whatsapp/logout', () => {
    it('returns success and triggers the logout', async () => {
      mockLogoutWhatsApp.mockResolvedValue(undefined);

      const res = await app.inject({ method: 'POST', url: '/whatsapp/logout' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ success: true });
      expect(mockLogoutWhatsApp).toHaveBeenCalledOnce();
    });

    it('returns 500 when the logout fails', async () => {
      mockLogoutWhatsApp.mockRejectedValueOnce(new Error('boom'));

      const res = await app.inject({ method: 'POST', url: '/whatsapp/logout' });

      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to logout WhatsApp session' });
    });
  });
});
