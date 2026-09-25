import { describe, it, expect } from 'vitest';
import { hasValidApiKey } from '../../src/utils/api-key.js';

describe('hasValidApiKey', () => {
  const key = 'secret-key-123';

  it('accepts the exact key', () => {
    expect(hasValidApiKey(key, key)).toBe(true);
  });

  it('rejects a wrong key of the same length', () => {
    expect(hasValidApiKey('secret-key-124', key)).toBe(false);
  });

  it('rejects a key of a different length', () => {
    expect(hasValidApiKey('secret', key)).toBe(false);
  });

  it('rejects a missing or empty header', () => {
    expect(hasValidApiKey(undefined, key)).toBe(false);
    expect(hasValidApiKey('', key)).toBe(false);
  });

  it('rejects a repeated header (array)', () => {
    expect(hasValidApiKey([key, key], key)).toBe(false);
  });

  it('does not throw on a multi-byte header with the same string length', () => {
    // Same .length in UTF-16, different byte length — timingSafeEqual would throw.
    const multiByte = 'é'.repeat(key.length);
    expect(() => hasValidApiKey(multiByte, key)).not.toThrow();
    expect(hasValidApiKey(multiByte, key)).toBe(false);
  });
});
