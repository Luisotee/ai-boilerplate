import type { WAMessage } from '@whiskeysockets/baileys';
import { extractPhoneFromJid, phoneFromJid, stripDeviceSuffix } from './jid.js';
import { logger } from '../logger.js';

/**
 * The display name the sender actually publishes, or undefined.
 *
 * Deliberately has NO identifier fallback: under v7 LID addressing the JID's
 * local part is an anonymized account id, and storing that as someone's profile
 * name renders a bare LID as if it were a person. Use this wherever the value
 * may reach `User.name`; use getSenderName() for message-row labels.
 */
export function getPushName(msg: WAMessage): string | undefined {
  return msg.pushName || msg.verifiedBizName || undefined;
}

/**
 * Get sender name from message.
 *
 * Always returns something — this labels the message row (group bubbles, the
 * content prefix), so a last-resort identifier beats an empty string. Prefers
 * the PN Baileys carries alongside a LID over the LID's meaningless digits.
 */
export function getSenderName(msg: WAMessage): string {
  const altPhone = phoneFromJid(
    stripDeviceSuffix(msg.key.participantAlt || msg.key.remoteJidAlt || '')
  );
  return (
    getPushName(msg) ?? altPhone ?? extractPhoneFromJid(msg.key.participant || msg.key.remoteJid!)
  );
}

/**
 * Extract contextInfo from any message type.
 * contextInfo lives on the specific message type (imageMessage, audioMessage, etc.),
 * not always on extendedTextMessage.
 */
function getContextInfo(msg: WAMessage) {
  const m = msg.message;
  if (!m) return null;

  return (
    m.extendedTextMessage?.contextInfo ??
    m.imageMessage?.contextInfo ??
    m.audioMessage?.contextInfo ??
    m.videoMessage?.contextInfo ??
    m.documentMessage?.contextInfo ??
    m.documentWithCaptionMessage?.message?.documentMessage?.contextInfo ??
    m.viewOnceMessage?.message?.imageMessage?.contextInfo ??
    m.viewOnceMessage?.message?.videoMessage?.contextInfo ??
    null
  );
}

/**
 * Check if bot is mentioned in group message
 * Supports both phone JID (@s.whatsapp.net) and LID (@lid) formats
 */
export function isBotMentioned(msg: WAMessage, botJid: string, botLid?: string): boolean {
  const contextInfo = getContextInfo(msg);
  const mentionedJids = contextInfo?.mentionedJid || [];
  const matchesJid = mentionedJids.includes(botJid);
  const matchesLid = botLid ? mentionedJids.includes(botLid) : false;

  logger.debug({ botJid, botLid, mentionedJids, matchesJid, matchesLid }, 'Checking bot mention');
  return matchesJid || matchesLid;
}

/**
 * Check if message is a reply to bot
 * Supports both phone JID (@s.whatsapp.net) and LID (@lid) formats
 */
export function isReplyToBotMessage(msg: WAMessage, botJid: string, botLid?: string): boolean {
  const quotedParticipant = getContextInfo(msg)?.participant;
  return quotedParticipant === botJid || (!!botLid && quotedParticipant === botLid);
}

/**
 * Determine if bot should respond in group chat
 * Checks for both @mention (JID or LID) and replies to bot messages
 */
export function shouldRespondInGroup(msg: WAMessage, botJid: string, botLid?: string): boolean {
  return isBotMentioned(msg, botJid, botLid) || isReplyToBotMessage(msg, botJid, botLid);
}
