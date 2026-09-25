import { describe, it, expect, beforeEach, vi } from 'vitest';
import type { WAVersion } from '@whiskeysockets/baileys';

const mockFetchLatestWaWebVersion = vi.fn();

vi.mock('@whiskeysockets/baileys', () => ({
  fetchLatestWaWebVersion: (...args: unknown[]) => mockFetchLatestWaWebVersion(...args),
}));

const LIVE_VERSION: WAVersion = [2, 3000, 1043324362];
// What Baileys hands back alongside `error` when the fetch fails — stale, and the
// reason a fetch failure must not be treated as a usable version.
const STALE_BUNDLED: WAVersion = [2, 3000, 1035194821];

// Re-imported per test: the module caches the resolved version in a singleton.
let getWaVersionConfig: () => Promise<{ version?: WAVersion }>;

describe('wa-version service', () => {
  beforeEach(async () => {
    vi.resetModules();
    mockFetchLatestWaWebVersion.mockReset();
    const mod = await import('../../src/services/wa-version.js');
    getWaVersionConfig = mod.getWaVersionConfig;
  });

  describe('on success', () => {
    it('should return the fetched version', async () => {
      mockFetchLatestWaWebVersion.mockResolvedValue({ version: LIVE_VERSION, isLatest: true });

      await expect(getWaVersionConfig()).resolves.toEqual({ version: LIVE_VERSION });
    });

    it('should cache the version and not re-fetch on the next call', async () => {
      mockFetchLatestWaWebVersion.mockResolvedValue({ version: LIVE_VERSION, isLatest: true });

      await getWaVersionConfig();
      const second = await getWaVersionConfig();

      expect(second).toEqual({ version: LIVE_VERSION });
      expect(mockFetchLatestWaWebVersion).toHaveBeenCalledTimes(1);
    });

    it('should pass an abort signal so a hanging fetch cannot stall reconnects', async () => {
      mockFetchLatestWaWebVersion.mockResolvedValue({ version: LIVE_VERSION, isLatest: true });

      await getWaVersionConfig();

      const [options] = mockFetchLatestWaWebVersion.mock.calls[0];
      expect(options.signal).toBeInstanceOf(AbortSignal);
    });
  });

  describe('on failure', () => {
    // Regression guard for the bug this module exists to prevent: Baileys merges config
    // with `{...DEFAULT_CONNECTION_CONFIG, ...config}`, so an explicit `version: undefined`
    // key overwrites its bundled default and crashes the handshake with a TypeError.
    // The key must be ABSENT, not merely undefined — `toEqual({})` and truthiness checks
    // both pass on `{version: undefined}`, so assert on the key itself.
    it('should omit the version key entirely when the fetch reports an error', async () => {
      mockFetchLatestWaWebVersion.mockResolvedValue({
        version: STALE_BUNDLED,
        isLatest: false,
        error: new Error('network down'),
      });

      const result = await getWaVersionConfig();

      expect('version' in result).toBe(false);
    });

    it('should omit the version key entirely when the fetch throws', async () => {
      mockFetchLatestWaWebVersion.mockRejectedValue(new Error('unexpected'));

      const result = await getWaVersionConfig();

      expect('version' in result).toBe(false);
    });

    it('should not return the stale bundled version reported alongside the error', async () => {
      mockFetchLatestWaWebVersion.mockResolvedValue({
        version: STALE_BUNDLED,
        isLatest: false,
        error: new Error('network down'),
      });

      const result = await getWaVersionConfig();

      expect(result.version).toBeUndefined();
    });

    it('should not cache a failure — a later call can still succeed', async () => {
      mockFetchLatestWaWebVersion.mockResolvedValueOnce({
        version: STALE_BUNDLED,
        isLatest: false,
        error: new Error('transient'),
      });
      mockFetchLatestWaWebVersion.mockResolvedValueOnce({ version: LIVE_VERSION, isLatest: true });

      const first = await getWaVersionConfig();
      const second = await getWaVersionConfig();

      expect('version' in first).toBe(false);
      expect(second).toEqual({ version: LIVE_VERSION });
      expect(mockFetchLatestWaWebVersion).toHaveBeenCalledTimes(2);
    });
  });
});
