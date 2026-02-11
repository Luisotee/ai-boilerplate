import type { WAMessage } from '@whiskeysockets/baileys';
import { extractPhoneFromJid } from './jid.js';
import { logger } from '../logger.js';

/**
 * Get sender name from message
 */
export function getSenderName(msg: WAMessage): string {
  return (
    msg.pushName ||
    msg.verifiedBizName ||
    extractPhoneFromJid(msg.key.participant || msg.key.remoteJid!)
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
