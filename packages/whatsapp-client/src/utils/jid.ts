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
  const [result] = await sock.onWhatsApp(identifier);

  if (!result?.exists) {
    throw new Error(`Phone number ${identifier} is not registered on WhatsApp`);
  }

  return result.jid;
}
