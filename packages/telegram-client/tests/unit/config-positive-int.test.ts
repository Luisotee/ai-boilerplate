import { describe, it, expect } from 'vitest';
import { positiveInt } from '../../src/config.js';

describe('positiveInt (CHAT_BURST_MAX_WAIT_MS)', () => {
  it('keeps a positive integer', () => {
    expect(positiveInt('5000', 8000)).toBe(5000);
  });

  it.each([undefined, '', '0', '-5', 'abc', '1.5'])('falls back on %s', (raw) => {
    expect(positiveInt(raw, 8000)).toBe(8000);
  });
});
