/**
 * TTL cache over `sock.groupMetadata`, used only to resolve a group's *subject*
 * — the conversation's display name in the dashboard.
 *
 * Deliberately NOT wired to Baileys' `cachedGroupMetadata` socket option. That
 * option is consumed inside `relayMessage`, where `participants` becomes the
 * device set an outgoing message is encrypted for (see `lib/Socket/
 * messages-send.js`). Serving a stale participant list there means a newly
 * added member cannot decrypt the bot's replies and a removed one still can.
 * Letting Baileys fetch its own metadata per send is the correct trade: this
 * cache exists for a display name and must never influence encryption.
 *
 * Nothing here throws, and no lookup may stall the message hot path — see
 * FETCH_TIMEOUT_MS.
 */

import type { GroupMetadata, WASocket } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

/** How long a resolved subject is trusted. Renames also evict eagerly via `invalidateGroup`. */
const OK_TTL_MS = 5 * 60_000;

/**
 * How long a *failed* lookup is remembered — much shorter than OK_TTL_MS.
 *
 * Short in absolute terms so a transient error self-heals, but long enough that
 * a backlog from a group the bot was removed from can't re-hit the network once
 * per message.
 */
const FAIL_TTL_MS = 30_000;

/**
 * Ceiling on a single `groupMetadata` round-trip.
 *
 * `handleTextMessage` awaits the subject before saving/answering, and
 * `messages.upsert` processes a batch serially — so an unbounded query would
 * stall every following message, including private ones. On timeout we return
 * undefined (the message proceeds, nameless) while the fetch keeps running to
 * warm the cache for the next message.
 */
const FETCH_TIMEOUT_MS = 3_000;

/** `data: null` is a negative entry: the fetch failed, don't retry until it expires. */
type Entry = { readonly data: GroupMetadata | null; readonly expiresAt: number };

const cache = new Map<string, Entry>();

/** De-dupes concurrent lookups of the same group into one network call. */
const inflight = new Map<string, Promise<GroupMetadata | undefined>>();

/**
 * Bumped by every invalidation. A fetch captures it before awaiting and only
 * writes if it still matches, so a result that resolves *after* an eviction
 * can't reinstate what was just dropped — which on relink would mean serving
 * the previous account's groups.
 */
let generation = 0;

function fresh(jid: string): Entry | undefined {
  const entry = cache.get(jid);
  if (!entry) return undefined;
  if (Date.now() >= entry.expiresAt) {
    cache.delete(jid);
    return undefined;
  }
  return entry;
}

const hit = (data: GroupMetadata): Entry => ({ data, expiresAt: Date.now() + OK_TTL_MS });
const miss = (): Entry => ({ data: null, expiresAt: Date.now() + FAIL_TTL_MS });

/** Read-through: cached value, else one fetch (shared across concurrent callers). */
export async function getGroupMetadataCached(
  sock: WASocket,
  jid: string
): Promise<GroupMetadata | undefined> {
  const cached = fresh(jid);
  if (cached) return cached.data ?? undefined;

  const pending = inflight.get(jid);
  if (pending) return pending;

  const gen = generation;
  const promise = (async () => {
    try {
      const data = await sock.groupMetadata(jid);
      if (gen === generation) cache.set(jid, hit(data));
      return data;
    } catch (error) {
      // Never throws: a missing subject must not stop a message being saved or
      // answered.
      logger.warn({ error, jid }, 'Group metadata fetch failed; continuing without subject');
      if (gen === generation) cache.set(jid, miss());
      return undefined;
    }
  })()
    // `.finally` on the promise, not a `finally` block: an async body runs
    // synchronously up to its first await, so a synchronous throw from
    // `sock.groupMetadata` would otherwise delete the entry *before*
    // `inflight.set` below adds it, stranding a resolved promise forever.
    .finally(() => inflight.delete(jid));

  inflight.set(jid, promise);
  return promise;
}

/** The group's display name (its subject), or undefined if it can't be resolved in time. */
export async function getGroupSubject(sock: WASocket, jid: string): Promise<string | undefined> {
  let timer: NodeJS.Timeout | undefined;
  const bounded = new Promise<undefined>((resolve) => {
    timer = setTimeout(() => {
      logger.warn({ jid, timeoutMs: FETCH_TIMEOUT_MS }, 'Group subject lookup timed out');
      resolve(undefined);
    }, FETCH_TIMEOUT_MS);
  });

  try {
    const meta = await Promise.race([getGroupMetadataCached(sock, jid), bounded]);
    return meta?.subject || undefined;
  } finally {
    clearTimeout(timer);
  }
}

/** Drop one group — call when its metadata is known to have changed. */
export function invalidateGroup(jid: string): void {
  generation++;
  cache.delete(jid);
}

/** Drop everything — call on socket teardown/relink, and in tests. */
export function clearGroupCache(): void {
  generation++;
  cache.clear();
  inflight.clear();
}
