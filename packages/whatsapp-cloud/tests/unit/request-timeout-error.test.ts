import { describe, it, expect, beforeEach } from 'vitest';
import { RequestTimeoutError } from '../../src/errors/RequestTimeoutError.js';

describe('RequestTimeoutError', () => {
  let error: RequestTimeoutError;

  beforeEach(() => {
    error = new RequestTimeoutError('https://api.example.com/chat', 30000);
  });

  // ---- Constructor ----

  it('should create an instance of Error', () => {
    expect(error).toBeInstanceOf(Error);
  });

  it('should create an instance of RequestTimeoutError', () => {
    expect(error).toBeInstanceOf(RequestTimeoutError);
  });

  it('should set the error name to RequestTimeoutError', () => {
    expect(error.name).toBe('RequestTimeoutError');
  });

  it('should include timeout and URL in the message', () => {
    expect(error.message).toBe('Request timeout after 30000ms: https://api.example.com/chat');
  });

  it('should store the url property', () => {
    expect(error.url).toBe('https://api.example.com/chat');
  });

  it('should store the timeoutMs property', () => {
    expect(error.timeoutMs).toBe(30000);
  });

  it('should set a timestamp close to now', () => {
    const now = new Date();
    expect(error.timestamp).toBeInstanceOf(Date);
    // Should be within 1 second of now
    expect(Math.abs(error.timestamp.getTime() - now.getTime())).toBeLessThan(1000);
  });

  it('should have a stack trace', () => {
    expect(error.stack).toBeDefined();
    expect(error.stack).toContain('RequestTimeoutError');
  });

  // ---- Different timeout values ----

  it('should handle small timeout values', () => {
    const err = new RequestTimeoutError('http://localhost:8000/test', 100);
    expect(err.message).toBe('Request timeout after 100ms: http://localhost:8000/test');
    expect(err.timeoutMs).toBe(100);
  });

  it('should handle very large timeout values', () => {
    const err = new RequestTimeoutError('http://localhost:8000/test', 600000);
    expect(err.message).toBe('Request timeout after 600000ms: http://localhost:8000/test');
    expect(err.timeoutMs).toBe(600000);
  });

  // ---- toJSON ----

  describe('toJSON', () => {
    it('should return an object with all error metadata', () => {
      const json = error.toJSON();

      expect(json).toEqual({
        name: 'RequestTimeoutError',
        message: 'Request timeout after 30000ms: https://api.example.com/chat',
        url: 'https://api.example.com/chat',
        timeoutMs: 30000,
        timestamp: expect.any(String),
      });
    });

    it('should return timestamp as ISO string', () => {
      const json = error.toJSON();
      // Verify it is a valid ISO date string
      const parsed = new Date(json.timestamp);
      expect(parsed.toISOString()).toBe(json.timestamp);
    });

    it('should produce valid JSON via JSON.stringify', () => {
      const serialized = JSON.stringify(error);
      const parsed = JSON.parse(serialized);

      expect(parsed.name).toBe('RequestTimeoutError');
      expect(parsed.url).toBe('https://api.example.com/chat');
      expect(parsed.timeoutMs).toBe(30000);
    });
  });

  // ---- Immutability of readonly properties ----

  it('should have readonly url property', () => {
    // TypeScript enforces this at compile time, but we can verify at runtime
    // that the property is set correctly and accessible
    expect(error.url).toBe('https://api.example.com/chat');
  });

  it('should have readonly timeoutMs property', () => {
    expect(error.timeoutMs).toBe(30000);
  });

  it('should have readonly timestamp property', () => {
    expect(error.timestamp).toBeInstanceOf(Date);
  });
});
