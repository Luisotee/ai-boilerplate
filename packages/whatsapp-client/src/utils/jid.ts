import type { WASocket } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { getBaileysSocket } from '../services/baileys.js';

/**
 * Strip device suffix from JID
 * Example: "5491126726818:50@s.whatsapp.net" -> "5491126726818@s.whatsapp.net"
 */
export function stripDeviceSuffix(jid: string): string {
  return jid.replace(/:\d+@/, '@');
}

/**
 * Check if JID is a group chat
 */
export function isGroupChat(jid: string): boolean {
  return jid.endsWith('@g.us');
}

/**
 * Extract phone number from JID
 */
export function extractPhoneFromJid(jid: string): string {
  return jid.split('@')[0];
}

/**
 * Extract E.164 phone number from a phone-based JID.
 * Returns null for LIDs and group JIDs.
 * Example: "5491126726818@s.whatsapp.net" -> "+5491126726818"
 */
export function phoneFromJid(jid: string): string | null {
  if (jid.endsWith('@s.whatsapp.net')) {
    return `+${jid.split('@')[0]}`;
  }
  return null;
}

/**
 * Check if JID is a LID (anonymized WhatsApp identifier)
 */
export function isLid(jid: string): boolean {
  return jid.endsWith('@lid');
}

/**
 * Resolve the real phone behind a LID via Baileys' LID↔PN mapping store.
 *
 * `getPNForLID` hands back a *device-suffixed* JID (`5511…:0@s.whatsapp.net`),
 * and `@hosted` for hosted LIDs — so the result must go through
 * stripDeviceSuffix before phoneFromJid, or this returns null every time.
 *
 * Never throws: a mapping miss must not stop a message from being handled.
 */
export async function resolveLidToPhone(sock: WASocket, lidJid: string): Promise<string | null> {
  if (!isLid(lidJid)) return null;

  try {
    // Optional-chained so a socket without the store (mocks, older Baileys)
    // degrades to "unknown phone" rather than a TypeError.
    const pnJid = await sock.signalRepository?.lidMapping?.getPNForLID(lidJid);
    return pnJid ? phoneFromJid(stripDeviceSuffix(pnJid)) : null;
  } catch (error) {
    logger.warn({ error, lidJid }, 'LID→PN lookup failed; continuing without phone');
    return null;
  }
}

/**
 * Resolve the E.164 phone for an incoming message's chat.
 *
 * Under Baileys v7 a chat is LID-addressed by default, so `remoteJid` is often
 * an `@lid` whose digits are an anonymized account id, NOT a phone. Sources, in
 * cost order:
 *   1. phone-based JID (`@s.whatsapp.net`) → its number directly
 *   2. the PN Baileys already carries on `key.remoteJidAlt` — free
 *   3. the LID↔PN mapping store — local cache, USync round-trip on a miss
 *
 * Returns undefined when the phone simply isn't knowable yet.
 */
export async function resolveSenderPhone(
  sock: WASocket,
  remoteJid: string,
  remoteJidAlt: string | null | undefined
): Promise<string | undefined> {
  const jid = stripDeviceSuffix(remoteJid);

  const direct = phoneFromJid(jid);
  if (direct) return direct;
  if (!isLid(jid)) return undefined;

  const alt = remoteJidAlt ? phoneFromJid(stripDeviceSuffix(remoteJidAlt)) : null;
  return alt ?? (await resolveLidToPhone(sock, jid)) ?? undefined;
}

/**
 * Check if string is already a JID
 * JIDs contain @ symbol (e.g., 1234567890@s.whatsapp.net, 123456-789@g.us)
 */
export function isJid(identifier: string): boolean {
  return identifier.includes('@');
}

/**
 * Normalize phone number or JID to standard JID format
 *
 * Accepts two formats:
 * - Phone number: 5511999999999 (country code + number, no symbols)
 * - JID: 5511999999999@s.whatsapp.net
 *
 * For phone numbers, uses Baileys onWhatsApp() to convert to JID and validate existence.
 * This handles WhatsApp's LID system automatically.
 *
 * @param identifier - Phone number (5511999999999) or JID (5511999999999@s.whatsapp.net)
 * @returns Normalized JID in format: number@s.whatsapp.net
 * @throws Error if number doesn't exist on WhatsApp
 *
 * @example
 * // Phone number
 * const jid = await normalizeJid('5511999999999');
 * // Returns: '5511999999999@s.whatsapp.net'
 *
 * @example
 * // Already a JID
 * const jid = await normalizeJid('5511999999999@s.whatsapp.net');
 * // Returns: '5511999999999@s.whatsapp.net'
 */
export async function normalizeJid(identifier: string): Promise<string> {
  // Already a JID, return as-is
  if (isJid(identifier)) {
    return identifier;
  }

  // Phone number - convert to JID using onWhatsApp
  const sock = getBaileysSocket();
  const response = await sock.onWhatsApp(identifier);

  if (response === undefined) {
    throw new Error(
      `WhatsApp lookup failed for ${identifier} (socket unavailable or request timed out)`
    );
  }

  const [result] = response;
  if (!result?.exists) {
    throw new Error(`Phone number ${identifier} is not registered on WhatsApp`);
  }

  return result.jid;
}
