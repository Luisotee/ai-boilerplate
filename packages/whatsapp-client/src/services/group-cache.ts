import type { GroupMetadata, WASocket } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

/**
 * TTL cache over `sock.groupMetadata`.
 *
 * A group's subject is needed on every incoming group message (it is the
 * conversation's display name), but fetching it per message would put a network
 * round-trip in the hot path. Entries are refreshed on a TTL and evicted
 * eagerly by the `groups.update` / `group-participants.update` listeners.
 *
 * Failures are cached too, for much longer than they are cheap to retry — a bot
 * removed from a group errors on every lookup, and without a negative entry
 * every message from that group's backlog would re-hit the network.
 */
const OK_TTL_MS = 5 * 60_000;
const FAIL_TTL_MS = 30_000;

type Entry = { data: GroupMetadata | null; expiresAt: number };

const cache = new Map<string, Entry>();
/** De-dupes concurrent lookups of the same group into one network call. */
const inflight = new Map<string, Promise<GroupMetadata | undefined>>();

function fresh(jid: string): Entry | undefined {
  const entry = cache.get(jid);
  if (!entry) return undefined;
  if (Date.now() >= entry.expiresAt) {
    cache.delete(jid);
    return undefined;
  }
  return entry;
}

/**
 * Pure cache read — never fetches.
 *
 * This is what Baileys' `cachedGroupMetadata` option wants: a synchronous-ish
 * peek it can use to skip its own fetch. Returning a fetch from here would put
 * a network call inside Baileys' send path, which is exactly what the option
 * exists to avoid.
 */
export async function peekGroupMetadata(jid: string): Promise<GroupMetadata | undefined> {
  return fresh(jid)?.data ?? undefined;
}

/** Read-through: cached value, else one fetch (shared across concurrent callers). */
export async function getGroupMetadataCached(
  sock: WASocket,
  jid: string
): Promise<GroupMetadata | undefined> {
  const cached = fresh(jid);
  if (cached) return cached.data ?? undefined;

  const pending = inflight.get(jid);
  if (pending) return pending;

  const promise = (async () => {
    try {
      const data = await sock.groupMetadata(jid);
      cache.set(jid, { data, expiresAt: Date.now() + OK_TTL_MS });
      return data;
    } catch (error) {
      // Never throws: a missing subject must not stop a message from being
      // saved or answered.
      logger.warn({ error, jid }, 'Group metadata fetch failed; continuing without subject');
      cache.set(jid, { data: null, expiresAt: Date.now() + FAIL_TTL_MS });
      return undefined;
    } finally {
      inflight.delete(jid);
    }
  })();

  inflight.set(jid, promise);
  return promise;
}

/** The group's display name (its subject), or undefined if it can't be resolved. */
export async function getGroupSubject(sock: WASocket, jid: string): Promise<string | undefined> {
  const meta = await getGroupMetadataCached(sock, jid);
  return meta?.subject || undefined;
}

/** Drop one group — call when its metadata is known to have changed. */
export function invalidateGroup(jid: string): void {
  cache.delete(jid);
}

/** Drop everything — call on socket teardown/relink, and in tests. */
export function clearGroupCache(): void {
  cache.clear();
  inflight.clear();
}
