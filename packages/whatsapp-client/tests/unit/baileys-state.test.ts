import { describe, it, expect, beforeEach } from 'vitest';
import type { WASocket } from '@whiskeysockets/baileys';

// Dynamic import to get a fresh module for each test suite.
// We use resetModules + dynamic import in beforeEach to reset the singleton state.
let getBaileysSocket: () => WASocket;
let setBaileysSocket: (sock: WASocket) => void;
let isBaileysReady: () => boolean;

describe('baileys state singleton', () => {
  beforeEach(async () => {
    // Re-import to reset module-level singleton state
    const { vi } = await import('vitest');
    vi.resetModules();
    const mod = await import('../../src/services/baileys.js');
    getBaileysSocket = mod.getBaileysSocket;
    setBaileysSocket = mod.setBaileysSocket;
    isBaileysReady = mod.isBaileysReady;
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

  describe('isBaileysReady', () => {
    it('should return false when socket is not set', () => {
      expect(isBaileysReady()).toBe(false);
    });

    it('should return true after socket is set', () => {
      const fakeSock = { ev: {} } as unknown as WASocket;

      setBaileysSocket(fakeSock);

      expect(isBaileysReady()).toBe(true);
    });
  });

  describe('integration: full lifecycle', () => {
    it('should transition from uninitialized to ready', () => {
      // Initially not ready
      expect(isBaileysReady()).toBe(false);
      expect(() => getBaileysSocket()).toThrow();

      // Set socket
      const fakeSock = { ev: {} } as unknown as WASocket;
      setBaileysSocket(fakeSock);

      // Now ready
      expect(isBaileysReady()).toBe(true);
      expect(getBaileysSocket()).toBe(fakeSock);
    });
  });
});
