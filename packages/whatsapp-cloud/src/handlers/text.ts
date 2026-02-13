import * as graphApi from '../services/graph-api.js';
import { sendMessageToAI, getUserPreferences, textToSpeech } from '../api-client.js';
import { phoneToJid, isGroupChat, phoneFromJid } from '../utils/jid.js';
import { logger } from '../logger.js';

interface ImageData {
  data: string; // base64 encoded
  mimetype: string;
}

interface DocumentData {
  data: string; // base64 encoded
  mimetype: string;
  filename: string;
}

interface HandleOptions {
  conversationType?: 'private' | 'group';
  senderJid?: string;
  senderName?: string;
  messageId?: string;
  isGroupAdmin?: boolean;
  saveOnly?: boolean;
}

/**
 * Handle incoming text messages (with optional image or document).
 * Adapted from the Baileys handler but uses Graph API for sending.
 *
 * @param to - Phone number of the sender (Cloud API format, e.g. "16505551234")
 * @param messageId - Cloud API message ID (wamid...)
 * @param text - Message text content
 * @param senderName - Display name of the sender
 * @param image - Optional base64-encoded image data
 * @param document - Optional base64-encoded document data
 * @param options - Additional message options
 */
export async function handleTextMessage(
  to: string,
  messageId: string,
  text: string,
  senderName: string,
  image?: ImageData,
  document?: DocumentData,
  options?: HandleOptions
): Promise<void> {
  const whatsappJid = phoneToJid(to);
  const phone = phoneFromJid(whatsappJid);
  const conversationType =
    options?.conversationType || (isGroupChat(whatsappJid) ? 'group' : 'private');
  const saveOnly = options?.saveOnly ?? false;

  // Save-only mode: persist message to history without generating a response
  if (saveOnly) {
    logger.debug({ whatsappJid, text: text.slice(0, 50) }, 'Saving group message to history');
    try {
      await sendMessageToAI(whatsappJid, text, {
        conversationType,
        senderJid: options?.senderJid,
        senderName,
        messageId,
        saveOnly: true,
        phone: phone ?? undefined,
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

  try {
    const response = await sendMessageToAI(whatsappJid, text, {
      conversationType,
      senderJid: options?.senderJid,
      senderName,
      messageId,
      isGroupAdmin: options?.isGroupAdmin,
      phone: phone ?? undefined,
      image,
      document,
    });

    if (!response) return; // saveOnly mode or empty response

    // Send text response via Graph API
    await graphApi.sendText(to, response);
    logger.info({ to: whatsappJid, responseLength: response.length }, 'Sent AI response');

    // Check if TTS is enabled and send voice message
    const prefs = await getUserPreferences(whatsappJid);
    if (prefs?.tts_enabled) {
      logger.info({ whatsappJid }, 'TTS enabled, generating voice message');
      const audioBuffer = await textToSpeech(response, whatsappJid);
      if (audioBuffer) {
        await graphApi.sendAudio(to, audioBuffer, 'audio/ogg; codecs=opus');
        logger.info({ whatsappJid }, 'Voice message sent');
      } else {
        logger.warn({ whatsappJid }, 'TTS failed, text-only sent');
      }
    }

    // Mark as read
    await graphApi.markAsRead(messageId);
  } catch (error) {
    logger.error({ error, to, messageId }, 'Error handling text message');
    try {
      await graphApi.sendReaction(to, messageId, '\u274c');
    } catch (reactionError) {
      logger.error({ error: reactionError }, 'Failed to send failure reaction');
    }
  }
}
