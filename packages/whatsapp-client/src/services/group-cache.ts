/**
 * The client's one cache of group metadata, with two read paths:
 *
 *  - per group (`getGroupMetadataCached`, 5 min TTL over `sock.groupMetadata`):
 *    the conversation's display name, and — under GROUP_GATING=membership —
 *    whether a group has a whitelisted member;
 *  - the whole fleet (`getParticipatingGroups`, 60 s TTL over
 *    `sock.groupFetchAllParticipating`): which groups a requesting user shares
 *    with the bot (`POST /whatsapp/shared-groups`). A fleet fetch also seeds the
 *    per-group entries, so both views come from the same data.
 *
 * Membership changes (`group-participants.update`) and renames (`groups.update`)
 * evict eagerly via `invalidateGroup`, which drops the fleet snapshot too.
 *
 * Deliberately NOT wired to Baileys' `cachedGroupMetadata` socket option. That
 * option is consumed inside `relayMessage`, where `participants` becomes the
 * device set an outgoing message is encrypted for (see `lib/Socket/
 * messages-send.js`). Serving a stale participant list there means a newly
 * added member cannot decrypt the bot's replies and a removed one still can.
 * Letting Baileys fetch its own metadata per send is the correct trade: this
 * cache must never influence encryption.
 *
 * Nothing here throws except `getParticipatingGroups` (its route must answer
 * 500, not an empty list), and no per-group lookup may stall the message hot
 * path — see FETCH_TIMEOUT_MS.
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

/**
 * The group's metadata, or undefined if it can't be resolved within `timeoutMs`.
 * The fetch keeps running after a timeout and warms the cache for next time.
 */
export async function getGroupMetadataBounded(
  sock: WASocket,
  jid: string,
  timeoutMs: number = FETCH_TIMEOUT_MS
): Promise<GroupMetadata | undefined> {
  let timer: NodeJS.Timeout | undefined;
  const bounded = new Promise<undefined>((resolve) => {
    timer = setTimeout(() => {
      logger.warn({ jid, timeoutMs }, 'Group metadata lookup timed out');
      resolve(undefined);
    }, timeoutMs);
  });
  try {
    return await Promise.race([getGroupMetadataCached(sock, jid), bounded]);
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Fleet snapshot: every group the bot participates in
// ---------------------------------------------------------------------------

/**
 * Short on purpose: this answers "which groups does this user share with the
 * bot", so a membership change must not leak for long. Eager eviction on
 * `group-participants.update` covers the common case; the TTL bounds the rest.
 */
const FLEET_TTL_MS = 60_000;

let fleet: { data: Record<string, GroupMetadata>; expiresAt: number } | null = null;
let fleetInflight: Promise<Record<string, GroupMetadata>> | null = null;

/**
 * Every group the bot is in, keyed by group JID (cached, de-duped).
 *
 * Throws when the query fails — unlike the per-group path, the caller here is
 * an authorization decision, and "no groups" must not be confused with "could
 * not check".
 */
export async function getParticipatingGroups(
  sock: WASocket
): Promise<Record<string, GroupMetadata>> {
  if (fleet && Date.now() < fleet.expiresAt) return fleet.data;
  if (fleetInflight) return fleetInflight;

  const gen = generation;
  const promise = (async () => {
    const data = await sock.groupFetchAllParticipating();
    if (gen === generation) {
      fleet = { data, expiresAt: Date.now() + FLEET_TTL_MS };
      for (const meta of Object.values(data)) cache.set(meta.id, hit(meta));
    }
    return data;
  })().finally(() => {
    fleetInflight = null;
  });

  fleetInflight = promise;
  return promise;
}

/**
 * Store metadata that just arrived complete (with its participant list), e.g.
 * the `groups.update` burst Baileys emits from inside `groupFetchAllParticipating`.
 *
 * Priming — not invalidating — is what keeps that burst from evicting the very
 * snapshot it belongs to: treating it as a change bumped the generation, so
 * the fleet result was never cached and every lookup refetched the whole fleet.
 */
export function primeGroup(meta: GroupMetadata): void {
  cache.set(meta.id, hit(meta));
}

/** True when a `groups.update` entry is a complete metadata object, not a partial change. */
export function isCompleteGroupMetadata(update: Partial<GroupMetadata>): update is GroupMetadata {
  return (
    typeof update.id === 'string' &&
    typeof update.subject === 'string' &&
    Array.isArray(update.participants)
  );
}

/** Drop one group (and the fleet snapshot) — call when its metadata is known to have changed. */
export function invalidateGroup(jid: string): void {
  generation++;
  cache.delete(jid);
  fleet = null;
}

/** Drop everything — call on socket teardown/relink, and in tests. */
export function clearGroupCache(): void {
  generation++;
  cache.clear();
  inflight.clear();
  fleet = null;
  fleetInflight = null;
}
