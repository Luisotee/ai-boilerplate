import { GraphApiError } from '../errors/GraphApiError.js';

/**
 * Map a failed send to the HTTP answer the AI API acts on. The broadcast
 * worker treats a 4xx as "nothing was sent" and a 5xx as "may have been
 * sent" (never retried), so the split has to be honest:
 *
 * - Graph 429: rate limited, nothing sent → 429 (retried later)
 * - other Graph 4xx: Meta refused the message (dead number, outside the 24h
 *   window, bad parameter) → 422
 * - a timeout, a Graph 5xx, or anything else → 502: the request may have
 *   reached Meta, so the message may be out
 *
 * The error string is generic on purpose: the Graph body can contain the
 * recipient's phone number.
 */
export function sendErrorResponse(err: unknown): { statusCode: 422 | 429 | 502; error: string } {
  if (err instanceof GraphApiError && err.status >= 400 && err.status < 500) {
    if (err.status === 429) return { statusCode: 429, error: 'Rate limited by the Graph API' };
    return { statusCode: 422, error: 'The Graph API rejected the message' };
  }
  return { statusCode: 502, error: 'Failed to send message; it may have been delivered' };
}
