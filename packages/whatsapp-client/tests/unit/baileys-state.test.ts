import { describe, it, expect, beforeEach } from 'vitest';
import type { WASocket } from '@whiskeysockets/baileys';

// Dynamic import to get a fresh module for each test suite.
// We use resetModules + dynamic import in beforeEach to reset the singleton state.
let getBaileysSocket: () => WASocket;
let setBaileysSocket: (sock: WASocket) => void;
let clearBaileysSocket: () => void;
let isBaileysReady: () => boolean;
let setSocketOpen: (open: boolean) => void;
let getLiveSocket: () => WASocket | null;
let setConnectionStatus: (s: 'connecting' | 'qr' | 'connected' | 'disconnected') => void;
let setLatestQr: (qr: string | null) => void;
let getConnectionInfo: () => {
  status: 'connecting' | 'qr' | 'connected' | 'disconnected';
  qr: string | null;
  qrGeneratedAt: string | null;
};

describe('baileys state singleton', () => {
  beforeEach(async () => {
    // Re-import to reset module-level singleton state
    const { vi } = await import('vitest');
    vi.resetModules();
    const mod = await import('../../src/services/baileys.js');
    getBaileysSocket = mod.getBaileysSocket;
    setBaileysSocket = mod.setBaileysSocket;
    clearBaileysSocket = mod.clearBaileysSocket;
    isBaileysReady = mod.isBaileysReady;
    setSocketOpen = mod.setSocketOpen;
    getLiveSocket = mod.getLiveSocket;
    setConnectionStatus = mod.setConnectionStatus;
    setLatestQr = mod.setLatestQr;
    getConnectionInfo = mod.getConnectionInfo;
  });

  describe('getBaileysSocket', () => {
    it('should throw when socket is not initialized', () => {
      expect(() => getBaileysSocket()).toThrow(
        'Baileys socket not initialized. Please scan QR code first.'
      );
    });

    it('should throw an Error instance when uninitialized', () => {
      expect(() => getBaileysSocket()).toThrow(Error);
    });
  });

  describe('setBaileysSocket', () => {
    it('should store the socket so getBaileysSocket returns it', () => {
      const fakeSock = { ev: {} } as unknown as WASocket;

      setBaileysSocket(fakeSock);

      expect(getBaileysSocket()).toBe(fakeSock);
    });

    it('should allow overwriting with a new socket', () => {
      const firstSock = { id: 'first' } as unknown as WASocket;
      const secondSock = { id: 'second' } as unknown as WASocket;

      setBaileysSocket(firstSock);
      expect(getBaileysSocket()).toBe(firstSock);

      setBaileysSocket(secondSock);
      expect(getBaileysSocket()).toBe(secondSock);
    });
  });

  describe('getLiveSocket', () => {
    it('should return null when no socket exists', () => {
      expect(getLiveSocket()).toBeNull();
    });

    it('should return a pre-open socket while isBaileysReady() is still false', () => {
      const fakeSock = { ev: {} } as unknown as WASocket;

      setBaileysSocket(fakeSock); // tracked at creation, before the connection opens

      expect(getLiveSocket()).toBe(fakeSock);
      expect(isBaileysReady()).toBe(false); // not "ready" until 'open'
    });
  });

  describe('isBaileysReady', () => {
    it('should return false when socket is not set', () => {
      expect(isBaileysReady()).toBe(false);
    });

    it('should return false when a socket exists but the connection is not open', () => {
      setBaileysSocket({ ev: {} } as unknown as WASocket);

      expect(isBaileysReady()).toBe(false);
    });

    it('should return true only after the socket is set AND the connection is open', () => {
      const fakeSock = { ev: {} } as unknown as WASocket;

      setBaileysSocket(fakeSock);
      setSocketOpen(true);

      expect(isBaileysReady()).toBe(true);
    });
  });

  describe('clearBaileysSocket', () => {
    it('should reset both the socket ref and the open flag', () => {
      const fakeSock = { ev: {} } as unknown as WASocket;
      setBaileysSocket(fakeSock);
      setSocketOpen(true);
      expect(isBaileysReady()).toBe(true);

      clearBaileysSocket();

      expect(isBaileysReady()).toBe(false);
      expect(getLiveSocket()).toBeNull();
      expect(() => getBaileysSocket()).toThrow();
    });
  });

  describe('connection info / pairing QR', () => {
    it('should default to connecting with no QR', () => {
      expect(getConnectionInfo()).toEqual({
        status: 'connecting',
        qr: null,
        qrGeneratedAt: null,
      });
    });

    it('should expose the QR and set status when a QR arrives', () => {
      setLatestQr('2@abc123def');
      setConnectionStatus('qr');

      const info = getConnectionInfo();
      expect(info.status).toBe('qr');
      expect(info.qr).toBe('2@abc123def');
      expect(info.qrGeneratedAt).not.toBeNull();
      // qrGeneratedAt is an ISO timestamp
      expect(() => new Date(info.qrGeneratedAt as string).toISOString()).not.toThrow();
    });

    it('should clear the QR and report connected once paired', () => {
      setLatestQr('2@abc123def');
      setConnectionStatus('qr');
      // simulate connection.update -> open
      setConnectionStatus('connected');
      setLatestQr(null);

      const info = getConnectionInfo();
      expect(info.status).toBe('connected');
      expect(info.qr).toBeNull();
      expect(info.qrGeneratedAt).toBeNull();
    });

    it('should report disconnected after a close', () => {
      setConnectionStatus('disconnected');
      expect(getConnectionInfo().status).toBe('disconnected');
    });
  });

  describe('integration: full lifecycle', () => {
    it('should transition uninitialized → created (pre-open) → ready', () => {
      // Initially not ready
      expect(isBaileysReady()).toBe(false);
      expect(() => getBaileysSocket()).toThrow();

      // Socket created (tracked at creation) but the connection isn't open yet
      const fakeSock = { ev: {} } as unknown as WASocket;
      setBaileysSocket(fakeSock);
      expect(getBaileysSocket()).toBe(fakeSock);
      expect(getLiveSocket()).toBe(fakeSock);
      expect(isBaileysReady()).toBe(false);

      // Connection opens → now ready
      setSocketOpen(true);
      expect(isBaileysReady()).toBe(true);
      expect(getBaileysSocket()).toBe(fakeSock);
    });
  });
});
