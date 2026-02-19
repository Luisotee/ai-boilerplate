import { describe, it, expect, beforeEach } from 'vitest';

// Dynamic import to get a fresh module for each test suite.
// We use resetModules + dynamic import in beforeEach to reset the singleton state.
let setCloudApiReady: (ready: boolean) => void;
let isCloudApiConnected: () => boolean;

describe('cloud-state singleton', () => {
  beforeEach(async () => {
    // Re-import to reset module-level singleton state
    const { vi } = await import('vitest');
    vi.resetModules();
    const mod = await import('../../src/services/cloud-state.js');
    setCloudApiReady = mod.setCloudApiReady;
    isCloudApiConnected = mod.isCloudApiConnected;
  });

  describe('isCloudApiConnected', () => {
    it('should return false by default', () => {
      expect(isCloudApiConnected()).toBe(false);
    });
  });

  describe('setCloudApiReady', () => {
    it('should set the state to ready when called with true', () => {
      setCloudApiReady(true);

      expect(isCloudApiConnected()).toBe(true);
    });

    it('should set the state to not ready when called with false', () => {
      // First make it ready
      setCloudApiReady(true);
      expect(isCloudApiConnected()).toBe(true);

      // Then set it back to not ready
      setCloudApiReady(false);
      expect(isCloudApiConnected()).toBe(false);
    });

    it('should handle being called multiple times with the same value', () => {
      setCloudApiReady(true);
      setCloudApiReady(true);

      expect(isCloudApiConnected()).toBe(true);
    });

    it('should reflect the most recent value', () => {
      setCloudApiReady(true);
      setCloudApiReady(false);
      setCloudApiReady(true);
      setCloudApiReady(false);

      expect(isCloudApiConnected()).toBe(false);
    });
  });

  describe('integration: full lifecycle', () => {
    it('should transition from not ready to ready and back', () => {
      // Initially not connected
      expect(isCloudApiConnected()).toBe(false);

      // Mark as ready
      setCloudApiReady(true);
      expect(isCloudApiConnected()).toBe(true);

      // Mark as not ready (e.g., config invalidated)
      setCloudApiReady(false);
      expect(isCloudApiConnected()).toBe(false);

      // Mark as ready again (e.g., config re-validated)
      setCloudApiReady(true);
      expect(isCloudApiConnected()).toBe(true);
    });
  });
});
