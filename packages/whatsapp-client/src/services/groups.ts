/**
 * Group-membership questions, answered from the group cache.
 *
 *  - `groupHasWhitelistedMember`: GROUP_GATING=membership — is this group in
 *    scope because one of its participants is whitelisted?
 *  - `findSharedGroups`: which groups does a requesting user share with the bot?
 *    Backs `POST /whatsapp/shared-groups`, which the AI API's shared-group tools
 *    call with identifiers taken from the user's DB row.
 *
 * Both match participants with the whitelist matcher (`utils/whitelist.ts`), so
 * there is exactly one definition of "this identifier refers to that person":
 * a phone entry matches a phone JID, a LID matches only verbatim, and digits
 * never cross namespaces (a LID's digits are not a phone).
 *
 * Every failure here can only cause a false NEGATIVE (a group not detected),
 * never a false-positive grant.
 */

import type { GroupMetadata, GroupParticipant, WASocket } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { isLid, stripDeviceSuffix } from '../utils/jid.js';
import { isWhitelisted, parseWhitelist, type Whitelist } from '../utils/whitelist.js';
import { getGroupMetadataBounded, getParticipatingGroups } from './group-cache.js';

/**
 * Ceiling on the metadata lookup behind a membership gate. Longer than the
 * subject lookup's 3s because a timeout here DROPS the message (fail closed)
 * rather than merely leaving it nameless; the fetch keeps running and warms
 * the cache for the next message.
 */
const GATE_TIMEOUT_MS = 10_000;

/** Every identifier a participant is known by, device suffix stripped. */
function participantIds(p: GroupParticipant): string[] {
  return [p.id, p.lid, p.phoneNumber]
    .filter((v): v is string => typeof v === 'string' && v.length > 0)
    .map(stripDeviceSuffix);
}

/**
 * The phone JID behind a LID participant, via Baileys' LID↔PN store.
 *
 * Quiet on a miss (debug, not warn): it runs once per LID participant of a
 * group, and a cold mapping is normal — `resolveLidToPhone` in utils/jid.ts
 * would log a warning for every one of them.
 */
async function lidToPhoneJid(sock: WASocket, lid: string): Promise<string | null> {
  try {
    const pn = await sock.signalRepository?.lidMapping?.getPNForLID?.(lid);
    if (!pn) return null;
    const stripped = stripDeviceSuffix(pn);
    return stripped.endsWith('@s.whatsapp.net') ? stripped : null;
  } catch (error) {
    logger.debug({ error, lid }, 'LID→PN lookup failed during group membership check');
    return null;
  }
}

/**
 * True if any participant matches `wl`: first a cheap synchronous pass over the
 * identifiers the metadata already carries, then — only on the no-match path —
 * the LID→PN fallback for LID-only participants.
 */
async function anyParticipantMatches(
  sock: WASocket,
  meta: GroupMetadata,
  wl: Whitelist
): Promise<boolean> {
  const participants = meta.participants ?? [];
  if (participants.some((p) => participantIds(p).some((id) => isWhitelisted(wl, id)))) {
    return true;
  }
  for (const p of participants) {
    if (p.phoneNumber || !p.id || !isLid(p.id)) continue;
    const pn = await lidToPhoneJid(sock, stripDeviceSuffix(p.id));
    if (pn && isWhitelisted(wl, pn)) return true;
  }
  return false;
}

/**
 * GROUP_GATING=membership: is `groupJid` in scope because it has a whitelisted
 * member? Fails closed — an unknown group, a failed lookup or a timeout all
 * answer false, and the caller skips the message.
 */
export async function groupHasWhitelistedMember(
  sock: WASocket,
  groupJid: string,
  wl: Whitelist
): Promise<boolean> {
  if (wl.size === 0) return true;
  const meta = await getGroupMetadataBounded(sock, groupJid, GATE_TIMEOUT_MS);
  if (!meta) {
    logger.warn({ groupJid }, 'Group metadata unavailable; treating group as out of scope');
    return false;
  }
  return anyParticipantMatches(sock, meta, wl);
}

export interface RequesterIdentity {
  jid?: string | null;
  lid?: string | null;
  phone?: string | null;
}

export interface SharedGroupInfo {
  groupJid: string;
  subject: string;
}

/**
 * A one-person "whitelist" of the requester's identifiers, so participant
 * matching reuses the exact rules of the real whitelist.
 */
function requesterMatcher(requester: RequesterIdentity): Whitelist {
  const entries = [requester.jid, requester.lid, requester.phone]
    .filter((v): v is string => typeof v === 'string' && v.trim().length > 0)
    .map((v) => stripDeviceSuffix(v.trim()))
    // A comma would split one identifier into two entries.
    .filter((v) => !v.includes(','));
  return parseWhitelist(entries.join(','));
}

/**
 * The groups (of those the bot participates in) that `requester` is a member
 * of. Throws if the fleet query fails — the route turns that into a 500 and
 * the AI API refuses, rather than reporting "no shared groups".
 */
export async function findSharedGroups(
  sock: WASocket,
  requester: RequesterIdentity
): Promise<SharedGroupInfo[]> {
  const matcher = requesterMatcher(requester);
  if (matcher.size === 0) return [];

  const groups = await getParticipatingGroups(sock);
  const shared: SharedGroupInfo[] = [];
  for (const [groupJid, meta] of Object.entries(groups)) {
    if (await anyParticipantMatches(sock, meta, matcher)) {
      shared.push({ groupJid, subject: meta.subject || groupJid });
    }
  }
  return shared;
}
