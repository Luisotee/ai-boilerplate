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
import { getBaileysSocket, getConnectionInfo, isBaileysReady } from '../../src/services/baileys.js';
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
        phoneNumber: null,
      });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/qr' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'qr',
        qr: '2@abc123def',
        qrGeneratedAt: '2026-05-26T14:00:00.000Z',
        phoneNumber: null,
      });
    });

    it('reports connected with no QR once paired, including paired phone number', async () => {
      mockGetConnectionInfo.mockReturnValue({
        status: 'connected',
        qr: null,
        qrGeneratedAt: null,
        phoneNumber: '5561999999999',
      });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/qr' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'connected',
        qr: null,
        qrGeneratedAt: null,
        phoneNumber: '5561999999999',
      });
    });

    it.each(['disconnected', 'connecting'] as const)('reports %s with no QR', async (status) => {
      mockGetConnectionInfo.mockReturnValue({
        status,
        qr: null,
        qrGeneratedAt: null,
        phoneNumber: null,
      });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/qr' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({ status, qr: null, qrGeneratedAt: null, phoneNumber: null });
    });
  });

  describe('GET /whatsapp/status', () => {
    it('returns connection info and paired phone number without qr', async () => {
      mockGetConnectionInfo.mockReturnValue({
        status: 'connected',
        qr: null,
        qrGeneratedAt: null,
        phoneNumber: '5561999999999',
      });

      const res = await app.inject({ method: 'GET', url: '/whatsapp/status' });

      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        status: 'connected',
        phoneNumber: '5561999999999',
      });
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

  describe('GET /whatsapp/reachout-timelock', () => {
    const mockIsReady = isBaileysReady as ReturnType<typeof vi.fn>;
    const mockGetSocket = getBaileysSocket as ReturnType<typeof vi.fn>;

    it('returns 503 when WhatsApp is not connected', async () => {
      mockIsReady.mockReturnValue(false);
      const res = await app.inject({ method: 'GET', url: '/whatsapp/reachout-timelock' });
      expect(res.statusCode).toBe(503);
    });

    it('reports an active timelock with its end time, and caches it per socket', async () => {
      const ends = new Date('2026-11-10T00:00:00.000Z');
      const sock = {
        fetchAccountReachoutTimelock: vi.fn().mockResolvedValue({
          isActive: true,
          timeEnforcementEnds: ends,
          enforcementType: 'DEFAULT',
        }),
      };
      mockIsReady.mockReturnValue(true);
      mockGetSocket.mockReturnValue(sock);

      const res = await app.inject({ method: 'GET', url: '/whatsapp/reachout-timelock' });
      expect(res.statusCode).toBe(200);
      expect(res.json()).toEqual({
        active: true,
        ends_at: ends.toISOString(),
        enforcement_type: 'DEFAULT',
      });

      await app.inject({ method: 'GET', url: '/whatsapp/reachout-timelock' });
      expect(sock.fetchAccountReachoutTimelock).toHaveBeenCalledOnce();
    });

    it('reports an inactive timelock', async () => {
      mockIsReady.mockReturnValue(true);
      mockGetSocket.mockReturnValue({
        fetchAccountReachoutTimelock: vi.fn().mockResolvedValue({ isActive: false }),
      });
      const res = await app.inject({ method: 'GET', url: '/whatsapp/reachout-timelock' });
      expect(res.json()).toEqual({ active: false, ends_at: null, enforcement_type: null });
    });

    it('returns 500 with a generic error when the query fails', async () => {
      mockIsReady.mockReturnValue(true);
      mockGetSocket.mockReturnValue({
        fetchAccountReachoutTimelock: vi.fn().mockRejectedValue(new Error('internal host x')),
      });
      const res = await app.inject({ method: 'GET', url: '/whatsapp/reachout-timelock' });
      expect(res.statusCode).toBe(500);
      expect(res.json()).toEqual({ error: 'Failed to fetch the reach-out timelock' });
    });
  });
});
