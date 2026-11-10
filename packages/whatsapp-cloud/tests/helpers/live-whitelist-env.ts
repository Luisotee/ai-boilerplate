/**
 * Test double for `services/live-whitelist.ts`: serves `WHITELIST_PHONES` from
 * the test's own env at call time (so `vi.stubEnv` keeps working), in place of
 * the AI API fetch. `WHITELIST_UNAVAILABLE=1` simulates an unreachable API.
 *
 *   vi.mock('../../src/services/live-whitelist.js', async () =>
 *     (await import('../helpers/live-whitelist-env.js')).envWhitelistMock()
 *   );
 */
export async function envWhitelistMock() {
  const { parseWhitelist } = await import('../../src/utils/whitelist.js');
  return {
    LIVE_WHITELIST_TTL_MS: 10_000,
    FAILURE_RETRY_MS: 2_000,
    getLiveWhitelist: async () =>
      process.env.WHITELIST_UNAVAILABLE === '1'
        ? null
        : parseWhitelist(process.env.WHITELIST_PHONES ?? ''),
    resetLiveWhitelist: () => {},
  };
}
