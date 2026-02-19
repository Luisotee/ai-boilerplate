import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { RequestTimeoutError } from '../../src/errors/RequestTimeoutError.js';

describe('RequestTimeoutError', () => {
  describe('constructor', () => {
    it('should create an instance with correct properties', () => {
      const error = new RequestTimeoutError('https://api.example.com/chat', 5000);

      expect(error.url).toBe('https://api.example.com/chat');
      expect(error.timeoutMs).toBe(5000);
      expect(error.name).toBe('RequestTimeoutError');
      expect(error.timestamp).toBeInstanceOf(Date);
    });

    it('should set a descriptive message including timeout and URL', () => {
      const error = new RequestTimeoutError('https://api.example.com/chat', 30000);

      expect(error.message).toBe('Request timeout after 30000ms: https://api.example.com/chat');
    });

    it('should be an instance of Error', () => {
      const error = new RequestTimeoutError('https://example.com', 1000);

      expect(error).toBeInstanceOf(Error);
      expect(error).toBeInstanceOf(RequestTimeoutError);
    });

    it('should have a stack trace', () => {
      const error = new RequestTimeoutError('https://example.com', 1000);

      expect(error.stack).toBeDefined();
      expect(error.stack).toContain('RequestTimeoutError');
    });

    it('should set timestamp to current time', () => {
      const before = new Date();
      const error = new RequestTimeoutError('https://example.com', 1000);
      const after = new Date();

      expect(error.timestamp.getTime()).toBeGreaterThanOrEqual(before.getTime());
      expect(error.timestamp.getTime()).toBeLessThanOrEqual(after.getTime());
    });

    it('should have readonly properties', () => {
      const error = new RequestTimeoutError('https://example.com', 1000);

      // TypeScript enforces readonly at compile time; at runtime we verify values are set
      expect(error.url).toBe('https://example.com');
      expect(error.timeoutMs).toBe(1000);
    });

    it('should handle zero timeout', () => {
      const error = new RequestTimeoutError('https://example.com', 0);

      expect(error.timeoutMs).toBe(0);
      expect(error.message).toBe('Request timeout after 0ms: https://example.com');
    });

    it('should handle very large timeout values', () => {
      const error = new RequestTimeoutError('https://example.com', 120000);

      expect(error.timeoutMs).toBe(120000);
      expect(error.message).toContain('120000ms');
    });

    it('should handle URLs with query parameters', () => {
      const url = 'https://api.example.com/chat?user=123&session=abc';
      const error = new RequestTimeoutError(url, 5000);

      expect(error.url).toBe(url);
      expect(error.message).toContain(url);
    });

    it('should handle empty URL string', () => {
      const error = new RequestTimeoutError('', 5000);

      expect(error.url).toBe('');
      expect(error.message).toBe('Request timeout after 5000ms: ');
    });
  });

  describe('toJSON', () => {
    it('should return a structured JSON object', () => {
      const error = new RequestTimeoutError('https://api.example.com/chat', 5000);
      const json = error.toJSON();

      expect(json).toHaveProperty('name', 'RequestTimeoutError');
      expect(json).toHaveProperty('message', 'Request timeout after 5000ms: https://api.example.com/chat');
      expect(json).toHaveProperty('url', 'https://api.example.com/chat');
      expect(json).toHaveProperty('timeoutMs', 5000);
      expect(json).toHaveProperty('timestamp');
    });

    it('should serialize timestamp as ISO string', () => {
      const error = new RequestTimeoutError('https://example.com', 1000);
      const json = error.toJSON();

      // ISO string format: YYYY-MM-DDTHH:mm:ss.sssZ
      expect(json.timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    });

    it('should be JSON.stringify-able', () => {
      const error = new RequestTimeoutError('https://example.com', 3000);
      const jsonString = JSON.stringify(error);
      const parsed = JSON.parse(jsonString);

      expect(parsed.name).toBe('RequestTimeoutError');
      expect(parsed.url).toBe('https://example.com');
      expect(parsed.timeoutMs).toBe(3000);
    });

    it('should not include stack trace in JSON output', () => {
      const error = new RequestTimeoutError('https://example.com', 1000);
      const json = error.toJSON();

      expect(json).not.toHaveProperty('stack');
    });

    it('should produce consistent output across multiple calls', () => {
      const error = new RequestTimeoutError('https://example.com', 2000);

      const json1 = error.toJSON();
      const json2 = error.toJSON();

      expect(json1).toEqual(json2);
    });
  });

  describe('error handling patterns', () => {
    it('should be catchable as Error', () => {
      try {
        throw new RequestTimeoutError('https://example.com', 5000);
      } catch (e) {
        expect(e).toBeInstanceOf(Error);
        expect((e as Error).message).toContain('timeout');
      }
    });

    it('should be identifiable by name property', () => {
      const error = new RequestTimeoutError('https://example.com', 5000);

      expect(error.name).toBe('RequestTimeoutError');
      // Useful for error type checking without instanceof
      expect(error.name !== 'Error').toBe(true);
    });

    it('should be distinguishable from regular Error', () => {
      const regularError = new Error('timeout');
      const timeoutError = new RequestTimeoutError('https://example.com', 5000);

      expect(regularError).not.toBeInstanceOf(RequestTimeoutError);
      expect(timeoutError).toBeInstanceOf(RequestTimeoutError);
    });
  });
});
