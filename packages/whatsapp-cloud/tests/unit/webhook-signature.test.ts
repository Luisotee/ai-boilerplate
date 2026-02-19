import { describe, it, expect } from 'vitest';
import crypto from 'node:crypto';
import { verifyWebhookSignature } from '../../src/utils/webhook-signature.js';

// ---------------------------------------------------------------------------
// Helper: compute the expected signature for a given payload + secret
// ---------------------------------------------------------------------------

function computeSignature(payload: string | Buffer, secret: string): string {
  return 'sha256=' + crypto.createHmac('sha256', secret).update(payload).digest('hex');
}

// ---------------------------------------------------------------------------
// verifyWebhookSignature
// ---------------------------------------------------------------------------

describe('verifyWebhookSignature', () => {
  const appSecret = 'test_app_secret_12345';
  const payload = '{"object":"whatsapp_business_account","entry":[]}';

  // ---- Happy path ----

  describe('valid signatures', () => {
    it('should return true for a valid HMAC-SHA256 signature (string payload)', () => {
      const signature = computeSignature(payload, appSecret);
      expect(verifyWebhookSignature(payload, signature, appSecret)).toBe(true);
    });

    it('should return true for a valid signature with Buffer payload', () => {
      const bufPayload = Buffer.from(payload, 'utf-8');
      const signature = computeSignature(bufPayload, appSecret);
      expect(verifyWebhookSignature(bufPayload, signature, appSecret)).toBe(true);
    });

    it('should return true for empty payload with matching signature', () => {
      const emptyPayload = '';
      const signature = computeSignature(emptyPayload, appSecret);
      expect(verifyWebhookSignature(emptyPayload, signature, appSecret)).toBe(true);
    });

    it('should return true for large payload with matching signature', () => {
      const largePayload = 'x'.repeat(100000);
      const signature = computeSignature(largePayload, appSecret);
      expect(verifyWebhookSignature(largePayload, signature, appSecret)).toBe(true);
    });

    it('should return true for payload with unicode characters', () => {
      const unicodePayload = '{"text":"Hello 世界 🌍"}';
      const signature = computeSignature(unicodePayload, appSecret);
      expect(verifyWebhookSignature(unicodePayload, signature, appSecret)).toBe(true);
    });
  });

  // ---- Tampered payload ----

  describe('tampered payload', () => {
    it('should return false when payload has been modified', () => {
      const signature = computeSignature(payload, appSecret);
      const tampered = payload.replace('whatsapp', 'telegram');
      expect(verifyWebhookSignature(tampered, signature, appSecret)).toBe(false);
    });

    it('should return false when a single character is changed', () => {
      const signature = computeSignature(payload, appSecret);
      const tampered = payload.slice(0, -1) + '}';
      // Only detects if the change actually alters the payload
      // The original already ends with }, so change a different char
      const tampered2 = 'X' + payload.slice(1);
      expect(verifyWebhookSignature(tampered2, signature, appSecret)).toBe(false);
    });

    it('should return false when payload has extra whitespace', () => {
      const signature = computeSignature(payload, appSecret);
      const tampered = payload + ' ';
      expect(verifyWebhookSignature(tampered, signature, appSecret)).toBe(false);
    });

    it('should return false when payload has newline appended', () => {
      const signature = computeSignature(payload, appSecret);
      const tampered = payload + '\n';
      expect(verifyWebhookSignature(tampered, signature, appSecret)).toBe(false);
    });
  });

  // ---- Wrong secret ----

  describe('wrong secret', () => {
    it('should return false when using a different app secret', () => {
      const signature = computeSignature(payload, appSecret);
      expect(verifyWebhookSignature(payload, signature, 'wrong_secret')).toBe(false);
    });

    it('should return false when secret is empty string', () => {
      const signature = computeSignature(payload, appSecret);
      expect(verifyWebhookSignature(payload, signature, '')).toBe(false);
    });

    it('should return false with similar but different secret', () => {
      const signature = computeSignature(payload, appSecret);
      expect(verifyWebhookSignature(payload, signature, appSecret + 'x')).toBe(false);
    });
  });

  // ---- Malformed signatures ----

  describe('malformed signatures', () => {
    it('should return false for empty signature string', () => {
      expect(verifyWebhookSignature(payload, '', appSecret)).toBe(false);
    });

    it('should return false for signature without sha256= prefix', () => {
      const sig = computeSignature(payload, appSecret);
      const noPrefix = sig.replace('sha256=', '');
      expect(verifyWebhookSignature(payload, noPrefix, appSecret)).toBe(false);
    });

    it('should return false for signature with wrong prefix', () => {
      const sig = computeSignature(payload, appSecret);
      const wrongPrefix = sig.replace('sha256=', 'sha1=');
      expect(verifyWebhookSignature(payload, wrongPrefix, appSecret)).toBe(false);
    });

    it('should return false for truncated signature (different length)', () => {
      const sig = computeSignature(payload, appSecret);
      const truncated = sig.slice(0, 20);
      expect(verifyWebhookSignature(payload, truncated, appSecret)).toBe(false);
    });

    it('should return false for extra-long signature', () => {
      const sig = computeSignature(payload, appSecret);
      const extraLong = sig + 'deadbeef';
      expect(verifyWebhookSignature(payload, extraLong, appSecret)).toBe(false);
    });

    it('should return false for completely random string', () => {
      expect(verifyWebhookSignature(payload, 'not-a-valid-signature-at-all', appSecret)).toBe(
        false
      );
    });
  });

  // ---- Timing-safe comparison (buffer length mismatch) ----

  describe('timing-safe comparison edge cases', () => {
    it('should handle signature that differs in length from expected (timingSafeEqual throws)', () => {
      // timingSafeEqual throws when buffers differ in length; the catch returns false
      const shortSig = 'sha256=abc';
      expect(verifyWebhookSignature(payload, shortSig, appSecret)).toBe(false);
    });

    it('should handle very long signature gracefully', () => {
      const longSig = 'sha256=' + 'a'.repeat(1000);
      expect(verifyWebhookSignature(payload, longSig, appSecret)).toBe(false);
    });
  });

  // ---- Different secrets produce different signatures ----

  describe('signature uniqueness', () => {
    it('should produce different results for different secrets on same payload', () => {
      const sig1 = computeSignature(payload, 'secret_a');
      const sig2 = computeSignature(payload, 'secret_b');
      expect(sig1).not.toBe(sig2);
    });

    it('should produce different results for same secret on different payloads', () => {
      const sig1 = computeSignature('payload_a', appSecret);
      const sig2 = computeSignature('payload_b', appSecret);
      expect(sig1).not.toBe(sig2);
    });
  });

  // ---- Cross-verification: string vs Buffer payload ----

  describe('string and Buffer payload consistency', () => {
    it('should verify string payload with signature computed from Buffer', () => {
      const bufPayload = Buffer.from(payload, 'utf-8');
      const signature = computeSignature(bufPayload, appSecret);
      // String and Buffer of same content should produce the same HMAC
      expect(verifyWebhookSignature(payload, signature, appSecret)).toBe(true);
    });

    it('should verify Buffer payload with signature computed from string', () => {
      const signature = computeSignature(payload, appSecret);
      const bufPayload = Buffer.from(payload, 'utf-8');
      expect(verifyWebhookSignature(bufPayload, signature, appSecret)).toBe(true);
    });
  });
});
