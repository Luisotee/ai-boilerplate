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
  parseDisplayPhoneNumber,
  _resetDisplayPhoneNumberCacheForTests,
} from '../../src/services/graph-api.js';

describe('parseDisplayPhoneNumber', () => {
  it('strips non-digits from valid phone numbers', () => {
    expect(parseDisplayPhoneNumber('+55 61 99999-9999')).toBe('5561999999999');
    expect(parseDisplayPhoneNumber('5511888888888')).toBe('5511888888888');
  });

  it('returns null for empty or missing inputs', () => {
    expect(parseDisplayPhoneNumber(null)).toBeNull();
    expect(parseDisplayPhoneNumber(undefined)).toBeNull();
    expect(parseDisplayPhoneNumber('')).toBeNull();
    expect(parseDisplayPhoneNumber('   ')).toBeNull();
    expect(parseDisplayPhoneNumber('no-digits')).toBeNull();
  });
});

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

  it('caches null when Graph API responds 200 with no display_phone_number (no refetch)', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ id: '12345' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    );

    const first = await getDisplayPhoneNumber();
    expect(first).toBeNull();
    expect(mockFetch).toHaveBeenCalledOnce();

    // Second call should use cached null and not call fetch again
    const second = await getDisplayPhoneNumber();
    expect(second).toBeNull();
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('returns already set phone number without network call', async () => {
    setDisplayPhoneNumber('5511888888888');

    const result = await getDisplayPhoneNumber();
    expect(result).toBe('5511888888888');
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('returns already set null without network call', async () => {
    setDisplayPhoneNumber(null);

    const result = await getDisplayPhoneNumber();
    expect(result).toBeNull();
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
