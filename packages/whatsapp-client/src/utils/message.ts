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
 * Check if bot is mentioned in group message
 */
export function isBotMentioned(msg: WAMessage, botJid: string): boolean {
  const mentionedJids = msg.message?.extendedTextMessage?.contextInfo?.mentionedJid || [];
  const matches = mentionedJids.includes(botJid);
  logger.debug({ botJid, mentionedJids, matches }, 'Checking bot mention');
  return matches;
}

/**
 * Check if message is a reply to bot
 */
export function isReplyToBotMessage(msg: WAMessage, botJid: string): boolean {
  const quotedParticipant = msg.message?.extendedTextMessage?.contextInfo?.participant;
  return quotedParticipant === botJid;
}

/**
 * Determine if bot should respond in group chat
 */
export function shouldRespondInGroup(msg: WAMessage, botJid: string): boolean {
  return isBotMentioned(msg, botJid) || isReplyToBotMessage(msg, botJid);
}
