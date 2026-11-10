import type { WAMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { sendSerialized } from '../services/baileys.js';

const FAILURE_EMOJI = '❌';

/**
 * Send a failure reaction (❌) to a message, on the live socket.
 */
export async function sendFailureReaction(msg: WAMessage): Promise<void> {
  try {
    const jid = msg.key.remoteJid;
    if (!jid) return;

    await sendSerialized(jid, {
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
