import type { WAMessageKey, WASocket } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { isSenderGroupAdmin } from '../utils/message.js';
import { getGroupMetadataCached } from './group-cache.js';

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
 * group's settings ("@bot stop the announcements here"), so both need it.
 *
 * - A slash command gets a live `groupMetadata` fetch: a just-demoted admin
 *   must not be able to `/clean all` on a stale cache.
 * - Any other addressed message reads the 5-minute metadata cache — it is about
 *   to cost an AI call anyway, and the only thing it can unlock is the
 *   low-stakes announcement opt-out.
 *
 * Returns `undefined` when the lookup fails, which the API treats as "not an
 * admin". Callers must not call this for un-addressed (saved-only) chatter.
 */
export async function resolveSenderGroupAdmin(
  sock: WASocket,
  groupJid: string,
  key: Pick<WAMessageKey, 'participant' | 'participantAlt'>,
  text: string
): Promise<boolean | undefined> {
  try {
    const metadata = looksLikeCommand(text)
      ? await sock.groupMetadata(groupJid)
      : await getGroupMetadataCached(sock, groupJid);
    if (!metadata) return undefined;
    const isAdmin = isSenderGroupAdmin(metadata.participants, key);
    logger.debug({ groupJid, isAdmin }, 'Checked group admin status');
    return isAdmin;
  } catch (error) {
    logger.warn({ error, groupJid }, 'Failed to check group admin status');
    return undefined;
  }
}
