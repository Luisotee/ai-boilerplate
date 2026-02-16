import crypto from 'node:crypto';

/**
 * Verify a Meta webhook signature using HMAC-SHA256.
 *
 * Meta signs webhook payloads with the app secret and sends the signature
 * in the X-Hub-Signature-256 header as "sha256=<hex>".
 *
 * @param payload - The raw request body (string or Buffer)
 * @param signature - The X-Hub-Signature-256 header value
 * @param appSecret - The Meta app secret used for HMAC verification
 * @returns true if the signature is valid, false otherwise
 */
export function verifyWebhookSignature(
  payload: string | Buffer,
  signature: string,
  appSecret: string
): boolean {
  const expectedSignature =
    'sha256=' + crypto.createHmac('sha256', appSecret).update(payload).digest('hex');

  try {
    return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expectedSignature));
  } catch {
    // timingSafeEqual throws if buffers have different lengths
    return false;
  }
}
