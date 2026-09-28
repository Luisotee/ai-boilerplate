import type { WAMessageKey, WASocket } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { isSenderGroupAdmin } from '../utils/message.js';
import { getGroupMetadataBounded } from './group-cache.js';

/** Longest an admin lookup may hold up a reply; a timeout counts as not-admin. */
export const ADMIN_LOOKUP_TIMEOUT_MS = 3_000;

/** Mirrors the AI API's `is_command`: a `/` after any leading @mentions. */
export function looksLikeCommand(text: string): boolean {
  return text
    .replace(/^(@\S+\s*)+/, '')
    .trimStart()
    .startsWith('/');
}

/**
 * Admin status of the sender of an ADDRESSED group message, for the AI API.
 *
 * The AI API fails closed (anything but `true` is refused) both for admin
 * commands (`/clean`, `/broadcast`, …) and for agent tools that change a
 * group's data or settings ("@bot clear this group's history", "@bot stop the
 * announcements here"), so both need it.
 *
 * - A slash command gets a live `groupMetadata` fetch.
 * - Any other addressed message reads the 5-minute metadata cache. That also
 *   gates `clean_user_data`, which stays safe because `whatsapp.ts` evicts the
 *   group on every `group-participants.update` (promote/demote included): a
 *   demoted admin can only slip through if that event itself was missed.
 *
 * Both lookups are bounded by `ADMIN_LOOKUP_TIMEOUT_MS` so a slow metadata
 * query can't stall the reply. Returns `undefined` when the lookup fails or
 * times out, which the API treats as "not an admin". Callers must not call
 * this for un-addressed (saved-only) chatter.
 */
export async function resolveSenderGroupAdmin(
  sock: WASocket,
  groupJid: string,
  key: Pick<WAMessageKey, 'participant' | 'participantAlt'>,
  text: string
): Promise<boolean | undefined> {
  try {
    const metadata = looksLikeCommand(text)
      ? await withTimeout(sock.groupMetadata(groupJid), ADMIN_LOOKUP_TIMEOUT_MS)
      : await getGroupMetadataBounded(sock, groupJid, ADMIN_LOOKUP_TIMEOUT_MS);
    if (!metadata) {
      logger.warn({ groupJid }, 'Group admin lookup timed out or failed');
      return undefined;
    }
    const isAdmin = isSenderGroupAdmin(metadata.participants, key);
    logger.debug({ groupJid, isAdmin }, 'Checked group admin status');
    return isAdmin;
  } catch (error) {
    logger.warn({ error, groupJid }, 'Failed to check group admin status');
    return undefined;
  }
}

/** Resolve with `undefined` once `ms` passes; the underlying call keeps running. */
async function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T | undefined> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<undefined>((resolve) => {
    timer = setTimeout(() => resolve(undefined), ms);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}
