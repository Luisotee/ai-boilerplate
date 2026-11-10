/**
 * The whitelist this client gates on, read live from the AI API.
 *
 * The AI API's effective value (`GET /chat/whitelist`: the `/admin` override,
 * else its `WHITELIST_PHONES` env) is the single source of truth, so an entry
 * added or removed through `PATCH /admin/settings` reaches this client within
 * about `LIVE_WHITELIST_TTL_MS` without a restart. This client's own
 * `WHITELIST_PHONES` env is no longer read.
 *
 * FAILS CLOSED: when the value can't be fetched (network error, timeout,
 * non-2xx, malformed body) `getLiveWhitelist()` resolves to `null` and callers
 * drop the message. There is no stale fallback past the TTL and no env
 * fallback. Every message needs the AI API to be saved or answered anyway.
 *
 * - A good value is cached for `LIVE_WHITELIST_TTL_MS`.
 * - Concurrent callers share one in-flight fetch.
 * - A failure is cached for `FAILURE_RETRY_MS`, so an outage costs one fetch per
 *   that interval rather than one per message, and is logged at most once per
 *   `WARN_EVERY_MS`.
 *
 * Byte-identical in whatsapp-client, whatsapp-cloud and telegram-client
 * (enforced by whatsapp-client `tests/unit/whitelist-copies.test.ts`): fix it
 * here, then `cp` it over.
 */
import { config } from '../config.js';
import { logger } from '../logger.js';
import { fetchWithTimeout } from '../utils/fetch.js';
import { parseWhitelist, type Whitelist } from '../utils/whitelist.js';

export const LIVE_WHITELIST_TTL_MS = 10_000;
export const FAILURE_RETRY_MS = 2_000;
const WARN_EVERY_MS = 60_000;

let cached: Whitelist | null = null;
let cachedRaw: string | undefined;
let expiresAt = 0;
let inflight: Promise<Whitelist | null> | null = null;
let lastWarnAt = -Infinity;

async function fetchWhitelist(): Promise<Whitelist | null> {
  try {
    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/chat/whitelist`,
      { headers: { 'X-API-Key': config.aiApiKey } },
      config.timeouts.polling
    );
    if (!response.ok) throw new Error(`AI API returned ${response.status}`);
    const body = (await response.json()) as { whitelist_phones?: unknown };
    if (typeof body?.whitelist_phones !== 'string') throw new Error('Malformed whitelist body');

    const raw = body.whitelist_phones;
    const wl = parseWhitelist(raw);
    if (raw !== cachedRaw) {
      logger.info(
        { entries: wl.size, enabled: wl.size > 0 },
        cachedRaw === undefined ? 'Whitelist loaded from the AI API' : 'Whitelist changed'
      );
      cachedRaw = raw;
    }
    cached = wl;
    expiresAt = Date.now() + LIVE_WHITELIST_TTL_MS;
    return wl;
  } catch (error) {
    cached = null;
    expiresAt = Date.now() + FAILURE_RETRY_MS;
    if (Date.now() - lastWarnAt >= WARN_EVERY_MS) {
      lastWarnAt = Date.now();
      logger.warn(
        { error },
        'Whitelist unavailable from the AI API: dropping messages (fail closed)'
      );
    }
    return null;
  }
}

/** The current whitelist, or `null` when it can't be fetched (callers must drop). */
export function getLiveWhitelist(): Promise<Whitelist | null> {
  if (Date.now() < expiresAt) return Promise.resolve(cached);
  if (!inflight) {
    inflight = fetchWhitelist().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

/** Tests only: forget the cached value. */
export function resetLiveWhitelist(): void {
  cached = null;
  cachedRaw = undefined;
  expiresAt = 0;
  inflight = null;
  lastWarnAt = -Infinity;
}
