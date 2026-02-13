/**
 * Phone number and JID conversion utilities for the WhatsApp Cloud API.
 * Cloud API uses plain phone numbers (e.g. "16505551234") in webhook payloads,
 * while the AI API uses JID format (e.g. "16505551234@s.whatsapp.net").
 */

/**
 * Convert a phone number to JID format.
 * Strips leading "+" if present and appends @s.whatsapp.net.
 *
 * @example phoneToJid('+16505551234') // '16505551234@s.whatsapp.net'
 * @example phoneToJid('16505551234')  // '16505551234@s.whatsapp.net'
 */
export function phoneToJid(phone: string): string {
  const cleaned = phone.startsWith('+') ? phone.slice(1) : phone;
  return `${cleaned}@s.whatsapp.net`;
}

/**
 * Extract the phone number part from a JID (everything before @).
 * Returns the identifier as-is if it contains no @.
 *
 * @example jidToPhone('16505551234@s.whatsapp.net') // '16505551234'
 * @example jidToPhone('16505551234')                // '16505551234'
 */
export function jidToPhone(jid: string): string {
  const atIndex = jid.indexOf('@');
  return atIndex === -1 ? jid : jid.slice(0, atIndex);
}

/**
 * Check if a JID represents a group chat.
 */
export function isGroupChat(jid: string): boolean {
  return jid.endsWith('@g.us');
}

/**
 * Strip device suffix from JID.
 * Example: "5491126726818:50@s.whatsapp.net" -> "5491126726818@s.whatsapp.net"
 */
export function stripDeviceSuffix(jid: string): string {
  return jid.replace(/:\d+@/, '@');
}

/**
 * Normalize an identifier to JID format.
 * If the identifier already contains @, return it as-is.
 * Otherwise, treat it as a phone number and convert to JID.
 *
 * @example normalizeJid('16505551234@s.whatsapp.net') // '16505551234@s.whatsapp.net'
 * @example normalizeJid('16505551234')                // '16505551234@s.whatsapp.net'
 */
export function normalizeJid(identifier: string): string {
  if (identifier.includes('@')) {
    return identifier;
  }
  return phoneToJid(identifier);
}

/**
 * Extract phone number from JID with "+" prefix (E.164 format).
 * Returns null for LIDs and group JIDs.
 *
 * @example phoneFromJid('16505551234@s.whatsapp.net') // '+16505551234'
 * @example phoneFromJid('123456-789@g.us')            // null
 */
export function phoneFromJid(jid: string): string | null {
  if (jid.endsWith('@s.whatsapp.net')) {
    return `+${jid.split('@')[0]}`;
  }
  return null;
}
