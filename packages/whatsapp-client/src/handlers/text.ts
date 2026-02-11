import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { getUserPreferences, sendMessageToAI, textToSpeech } from '../api-client.js';
import { stripDeviceSuffix, isGroupChat } from '../utils/jid.js';
import { getSenderName } from '../utils/message.js';
import { sendFailureReaction } from '../utils/reactions.js';

interface ImageData {
  buffer: Buffer;
  mimetype: string;
}

interface DocumentData {
  buffer: Buffer;
  mimetype: string;
  filename: string;
}

interface HandleOptions {
  /** When true, saves message to history without generating a response */
  saveOnly?: boolean;
  /** Whether the sender is a group admin (for admin-only commands) */
  isGroupAdmin?: boolean;
}

/**
 * Handle incoming text messages (with optional image or document)
 */
export async function handleTextMessage(
  sock: WASocket,
  msg: WAMessage,
  text: string,
  image?: ImageData,
  document?: DocumentData,
  options?: HandleOptions
): Promise<void> {
  const whatsappJid = stripDeviceSuffix(msg.key.remoteJid!);
  const conversationType = isGroupChat(whatsappJid) ? 'group' : 'private';
  const saveOnly = options?.saveOnly ?? false;
  const isGroupAdmin = options?.isGroupAdmin;

  // Save-only mode: persist message to history without generating a response
  if (saveOnly) {
    logger.debug({ whatsappJid, text: text.slice(0, 50) }, 'Saving group message to history');
    try {
      await sendMessageToAI(whatsappJid, text, {
        conversationType,
        senderJid: msg.key.participant ?? undefined,
        senderName: getSenderName(msg),
        messageId: msg.key.id ?? undefined,
        saveOnly: true,
      });
    } catch (error) {
      logger.warn({ error, whatsappJid }, 'Failed to save group message to history');
    }
    return;
  }

  logger.info(
    { from: whatsappJid, text, conversationType, hasImage: !!image, hasDocument: !!document },
    'Received message'
  );

  // Send typing indicator
  await sock.sendPresenceUpdate('composing', whatsappJid);

  try {
    const response = await sendMessageToAI(whatsappJid, text, {
      conversationType,
      senderJid: msg.key.participant ?? undefined,
      senderName: getSenderName(msg),
      messageId: msg.key.id ?? undefined,
      isGroupAdmin,
      image: image
        ? {
            data: image.buffer.toString('base64'),
            mimetype: image.mimetype,
          }
        : undefined,
      document: document
        ? {
            data: document.buffer.toString('base64'),
            mimetype: document.mimetype,
            filename: document.filename,
          }
        : undefined,
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
