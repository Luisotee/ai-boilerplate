import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

const FAILURE_EMOJI = '❌';

/**
 * Send a failure reaction (❌) to a message
 */
export async function sendFailureReaction(sock: WASocket, msg: WAMessage): Promise<void> {
  try {
    const jid = msg.key.remoteJid;
    if (!jid) return;

    await sock.sendMessage(jid, {
      react: {
        text: FAILURE_EMOJI,
        key: msg.key,
      },
    });
    logger.debug({ jid, messageId: msg.key.id }, 'Sent failure reaction');
  } catch (error) {
    logger.error({ error }, 'Failed to send failure reaction');
  }
}
