import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

vi.mock('../../src/config.js', () => ({
  config: {
    logLevel: 'silent',
    meta: {
      phoneNumberId: 'TEST_PHONE_ID',
      accessToken: 'TEST_TOKEN',
      graphApiVersion: 'v21.0',
      graphApiBaseUrl: 'https://graph.facebook.com',
    },
    timeouts: {
      default: 5000,
    },
  },
}));

import {
  getDisplayPhoneNumber,
  setDisplayPhoneNumber,
  _resetDisplayPhoneNumberCacheForTests,
} from '../../src/services/graph-api.js';

describe('getDisplayPhoneNumber', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetDisplayPhoneNumberCacheForTests();
  });

  it('fetches from Graph API, sanitizes digits, and caches the result', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ display_phone_number: '+55 61 99999-9999' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    );

    const first = await getDisplayPhoneNumber();
    expect(first).toBe('5561999999999');
    expect(mockFetch).toHaveBeenCalledOnce();

    // Second call should use cache without fetching again
    const second = await getDisplayPhoneNumber();
    expect(second).toBe('5561999999999');
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('returns already set phone number without network call', async () => {
    setDisplayPhoneNumber('5511888888888');

    const result = await getDisplayPhoneNumber();
    expect(result).toBe('5511888888888');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('returns null and does not throw when Graph API responds with an error', async () => {
    mockFetch.mockResolvedValueOnce(new Response('Invalid token', { status: 401 }));

    const result = await getDisplayPhoneNumber();
    expect(result).toBeNull();
  });

  it('returns null and handles network failure gracefully', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Network error'));

    const result = await getDisplayPhoneNumber();
    expect(result).toBeNull();
  });
});
