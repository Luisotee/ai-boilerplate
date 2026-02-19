import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { RequestTimeoutError } from '../../src/errors/RequestTimeoutError.js';

// We need to mock globalThis.fetch before importing the module under test.
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

const { fetchWithTimeout } = await import('../../src/utils/fetch.js');

describe('fetchWithTimeout', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockFetch.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('should return the response on a successful fetch', async () => {
    const fakeResponse = new Response('ok', { status: 200 });
    mockFetch.mockResolvedValueOnce(fakeResponse);

    const result = await fetchWithTimeout('https://example.com/api', {}, 5000);

    expect(result).toBe(fakeResponse);
    expect(mockFetch).toHaveBeenCalledOnce();
    expect(mockFetch).toHaveBeenCalledWith('https://example.com/api', expect.objectContaining({
      signal: expect.any(AbortSignal),
    }));
  });

  it('should pass options through to fetch along with the abort signal', async () => {
    const fakeResponse = new Response('ok', { status: 200 });
    mockFetch.mockResolvedValueOnce(fakeResponse);

    const headers = { 'Content-Type': 'application/json' };
    await fetchWithTimeout('https://example.com/api', { method: 'POST', headers }, 10000);

    expect(mockFetch).toHaveBeenCalledWith('https://example.com/api', expect.objectContaining({
      method: 'POST',
      headers,
      signal: expect.any(AbortSignal),
    }));
  });

  it('should throw RequestTimeoutError when the request times out', async () => {
    // Simulate fetch that never resolves until aborted
    mockFetch.mockImplementation((_url: string, options: RequestInit) => {
      return new Promise((_resolve, reject) => {
        options.signal?.addEventListener('abort', () => {
          const abortError = new Error('The operation was aborted');
          abortError.name = 'AbortError';
          reject(abortError);
        });
      });
    });

    const fetchPromise = fetchWithTimeout('https://slow-api.com/data', {}, 3000);

    // Advance timers to trigger the abort
    vi.advanceTimersByTime(3000);

    await expect(fetchPromise).rejects.toThrow(RequestTimeoutError);
    await expect(fetchPromise).rejects.toThrow('Request timeout after 3000ms: https://slow-api.com/data');
  });

  it('should rethrow non-abort errors as-is', async () => {
    const networkError = new Error('Network failure');
    networkError.name = 'TypeError';
    mockFetch.mockRejectedValueOnce(networkError);

    await expect(
      fetchWithTimeout('https://example.com/api', {}, 5000)
    ).rejects.toThrow(networkError);
  });

  it('should rethrow errors that are not Error instances', async () => {
    mockFetch.mockRejectedValueOnce('string error');

    await expect(
      fetchWithTimeout('https://example.com/api', {}, 5000)
    ).rejects.toBe('string error');
  });

  it('should pass an AbortController signal to fetch', async () => {
    const fakeResponse = new Response('ok', { status: 200 });
    mockFetch.mockResolvedValueOnce(fakeResponse);

    await fetchWithTimeout('https://example.com/api', {}, 5000);

    const callArgs = mockFetch.mock.calls[0];
    const passedOptions = callArgs[1] as RequestInit;
    expect(passedOptions.signal).toBeInstanceOf(AbortSignal);
  });

  it('should clear the timeout after a successful response', async () => {
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
    const fakeResponse = new Response('ok', { status: 200 });
    mockFetch.mockResolvedValueOnce(fakeResponse);

    await fetchWithTimeout('https://example.com/api', {}, 5000);

    expect(clearTimeoutSpy).toHaveBeenCalled();
    clearTimeoutSpy.mockRestore();
  });

  it('should clear the timeout after a fetch error', async () => {
    const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
    mockFetch.mockRejectedValueOnce(new Error('fail'));

    await expect(
      fetchWithTimeout('https://example.com/api', {}, 5000)
    ).rejects.toThrow('fail');

    expect(clearTimeoutSpy).toHaveBeenCalled();
    clearTimeoutSpy.mockRestore();
  });

  it('should use default empty options when none provided', async () => {
    const fakeResponse = new Response('ok', { status: 200 });
    mockFetch.mockResolvedValueOnce(fakeResponse);

    const result = await fetchWithTimeout('https://example.com/api', undefined, 5000);

    expect(result).toBe(fakeResponse);
    expect(mockFetch).toHaveBeenCalledWith('https://example.com/api', expect.objectContaining({
      signal: expect.any(AbortSignal),
    }));
  });

  it('should not override a user-provided signal (spread behavior)', async () => {
    // The implementation spreads options then adds signal, so the controller's signal wins
    const userController = new AbortController();
    const fakeResponse = new Response('ok', { status: 200 });
    mockFetch.mockResolvedValueOnce(fakeResponse);

    await fetchWithTimeout('https://example.com/api', { signal: userController.signal }, 5000);

    const callArgs = mockFetch.mock.calls[0];
    const passedOptions = callArgs[1] as RequestInit;
    // The internal AbortController's signal should override the user's signal
    expect(passedOptions.signal).not.toBe(userController.signal);
  });
});
