import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { getUserPreferences, sendMessageToAI, textToSpeech } from '../api-client.js';
import { stripDeviceSuffix, isGroupChat } from '../utils/jid.js';
import { getSenderName, shouldRespondInGroup } from '../utils/message.js';
import { sendFailureReaction } from '../utils/reactions.js';

/**
 * Handle incoming text messages
 */
export async function handleTextMessage(
  sock: WASocket,
  msg: WAMessage,
  text: string
): Promise<void> {
  const whatsappJid = stripDeviceSuffix(msg.key.remoteJid!);
  const conversationType = isGroupChat(whatsappJid) ? 'group' : 'private';
  const botJid = stripDeviceSuffix(sock.user!.id);

  // In groups, only respond if mentioned or replied to
  if (conversationType === 'group' && !shouldRespondInGroup(msg, botJid)) {
    logger.debug({ whatsappJid }, 'Skipping group message (not mentioned)');
    return;
  }

  logger.info({ from: whatsappJid, text, conversationType }, 'Received message');

  // Send typing indicator
  await sock.sendPresenceUpdate('composing', whatsappJid);

  try {
    const response = await sendMessageToAI(whatsappJid, text, {
      conversationType,
      senderJid: msg.key.participant,
      senderName: getSenderName(msg),
      messageId: msg.key.id,
    });

    // Send text response first
    await sock.sendMessage(whatsappJid, { text: response });
    logger.info({ to: whatsappJid, responseLength: response.length }, 'Sent AI response');

    // Check if TTS is enabled and send voice message
    if (response) {
      const prefs = await getUserPreferences(whatsappJid);
      if (prefs?.tts_enabled) {
        logger.info({ whatsappJid }, 'TTS enabled, generating voice message');
        await sock.sendPresenceUpdate('recording', whatsappJid);
        const audioBuffer = await textToSpeech(response, whatsappJid);
        if (audioBuffer) {
          await sock.sendMessage(whatsappJid, {
            audio: audioBuffer,
            mimetype: 'audio/ogg; codecs=opus',
            ptt: true, // Voice note
          });
          logger.info({ whatsappJid }, 'Voice message sent');
        } else {
          logger.warn({ whatsappJid }, 'TTS failed, text-only sent');
        }
      }
    }
  } catch (error) {
    logger.error({ error, whatsappJid }, 'Error processing message');
    await sendFailureReaction(sock, msg);
    await sock.sendMessage(whatsappJid, {
      text: 'Sorry, I encountered an error processing your message. Please try again.',
    });
  } finally {
    await sock.sendPresenceUpdate('paused', whatsappJid);
  }
}
