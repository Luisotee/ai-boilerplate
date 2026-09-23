import crypto from 'node:crypto';

/**
 * Constant-time check of an `X-API-Key` header against the configured key.
 *
 * Shared by the auth hook and the rate limiter's allowList: authenticated
 * inter-service calls all come from one IP (the AI API / worker), so a shared
 * per-IP budget would 429 — and lose — legitimate bot traffic during a burst.
 *
 * Compares byte lengths, not string lengths: a multi-byte header with the same
 * string length would otherwise make `timingSafeEqual` throw (→ HTTP 500).
 */
export function hasValidApiKey(header: string | string[] | undefined, expected: string): boolean {
  if (typeof header !== 'string' || header.length === 0) return false;
  const given = Buffer.from(header);
  const wanted = Buffer.from(expected);
  return given.length === wanted.length && crypto.timingSafeEqual(given, wanted);
}
