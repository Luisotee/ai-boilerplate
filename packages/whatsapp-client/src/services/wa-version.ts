import { fetchLatestWaWebVersion } from '@whiskeysockets/baileys';
import type { WAVersion } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

// Baileys bundles a hardcoded WhatsApp Web version that goes stale within months; once
// WhatsApp rejects it, pairing dies with a 405 at the handshake before any QR is emitted.
// Fetching the live version keeps pairing working without a version pinned in source.

/** `fetchLatestWaWebVersion` has no built-in timeout and inherits undici's (~300s headers
 *  timeout). This runs on every reconnect, so a hanging network would stall the backoff
 *  loop for minutes without it. Matches the 5s the AI API uses proxying /admin/whatsapp/qr. */
const FETCH_TIMEOUT_MS = 5000;

let cachedWaVersion: WAVersion | undefined;

/**
 * Resolve the current WhatsApp Web version as a config fragment to spread into
 * `makeWASocket`.
 *
 * Returns `{}` — never `{ version: undefined }` — when the version can't be resolved.
 * Baileys merges config as `{ ...DEFAULT_CONNECTION_CONFIG, ...config }`, so an explicit
 * `version: undefined` key would clobber its bundled default and crash the handshake with
 * a TypeError. Omitting the key is what lets the default apply.
 */
export async function getWaVersionConfig(): Promise<{ version?: WAVersion }> {
  if (cachedWaVersion) return { version: cachedWaVersion };

  try {
    const { version, error } = await fetchLatestWaWebVersion({
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });

    // Resolves rather than rejects on failure, reporting the problem via `error` and
    // handing back its stale bundled version — which is what the 405 is. Don't cache it.
    if (error) {
      logger.warn({ err: error }, 'Could not fetch WA Web version; using bundled default');
      return {};
    }

    cachedWaVersion = version;
    logger.info({ version }, 'Resolved WhatsApp Web version');
    return { version };
  } catch (err) {
    // Belt-and-suspenders: the library always resolves, so this is not the load-bearing
    // failure path — the `error` field above is.
    logger.warn({ err }, 'Could not fetch WA Web version; using bundled default');
    return {};
  }
}
