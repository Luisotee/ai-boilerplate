import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../src/logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));
vi.mock('../../src/utils/fetch.js', () => ({ fetchWithTimeout: vi.fn() }));

import { fetchWithTimeout } from '../../src/utils/fetch.js';
import { logger } from '../../src/logger.js';
import {
  FAILURE_RETRY_MS,
  LIVE_WHITELIST_TTL_MS,
  getLiveWhitelist,
  resetLiveWhitelist,
} from '../../src/services/live-whitelist.js';

const fetchMock = vi.mocked(fetchWithTimeout);

function ok(value: unknown) {
  return { ok: true, status: 200, json: async () => value } as unknown as Response;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  vi.clearAllMocks();
  resetLiveWhitelist();
});

afterEach(() => vi.useRealTimers());

describe('getLiveWhitelist', () => {
  it('fetches GET /chat/whitelist with the API key and parses it', async () => {
    fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: '5511999999999, tg:42' }));
    const wl = await getLiveWhitelist();
    expect(wl?.size).toBe(2);
    expect(wl?.phones.has('5511999999999')).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/chat\/whitelist$/);
    expect((init as RequestInit).headers).toHaveProperty('X-API-Key');
  });

  it('an empty value means disabled (size 0), not unavailable', async () => {
    fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: '' }));
    const wl = await getLiveWhitelist();
    expect(wl).not.toBeNull();
    expect(wl?.size).toBe(0);
  });

  it('serves the cached value within the TTL and refetches after it', async () => {
    fetchMock.mockResolvedValue(ok({ whitelist_phones: '5511999999999' }));
    await getLiveWhitelist();
    vi.advanceTimersByTime(LIVE_WHITELIST_TTL_MS - 1);
    await getLiveWhitelist();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    await getLiveWhitelist();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('picks up a change on the next refresh', async () => {
    fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: '5511999999999' }));
    fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: '5511999999999,5511888888888' }));
    expect((await getLiveWhitelist())?.size).toBe(1);
    vi.advanceTimersByTime(LIVE_WHITELIST_TTL_MS);
    expect((await getLiveWhitelist())?.size).toBe(2);
  });

  it('shares one in-flight fetch between concurrent callers', async () => {
    let resolve!: (r: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise((r) => (resolve = r)));
    const a = getLiveWhitelist();
    const b = getLiveWhitelist();
    resolve(ok({ whitelist_phones: 'tg:1' }));
    expect(await a).toBe(await b);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a network error', () => fetchMock.mockRejectedValueOnce(new Error('ECONNREFUSED'))],
    [
      'a non-2xx',
      () => fetchMock.mockResolvedValueOnce({ ok: false, status: 500 } as unknown as Response),
    ],
    ['a malformed body', () => fetchMock.mockResolvedValueOnce(ok({ nope: 1 }))],
    ['a non-string value', () => fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: null }))],
  ])('FAILS CLOSED (null) on %s', async (_label, arrange) => {
    arrange();
    expect(await getLiveWhitelist()).toBeNull();
  });

  it('never serves a stale value after a failure', async () => {
    fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: '5511999999999' }));
    fetchMock.mockRejectedValueOnce(new Error('down'));
    expect(await getLiveWhitelist()).not.toBeNull();
    vi.advanceTimersByTime(LIVE_WHITELIST_TTL_MS);
    expect(await getLiveWhitelist()).toBeNull();
  });

  it('caches a failure briefly, then retries, and warns at most once a minute', async () => {
    fetchMock.mockRejectedValue(new Error('down'));
    await getLiveWhitelist();
    await getLiveWhitelist();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(FAILURE_RETRY_MS);
    await getLiveWhitelist();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(logger.warn).toHaveBeenCalledTimes(1);

    fetchMock.mockResolvedValueOnce(ok({ whitelist_phones: '' }));
    vi.advanceTimersByTime(FAILURE_RETRY_MS);
    expect(await getLiveWhitelist()).not.toBeNull();
  });
});
