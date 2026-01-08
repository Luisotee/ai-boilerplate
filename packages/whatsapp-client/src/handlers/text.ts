import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';
import { sendMessageToAI } from '../api-client.js';
import { stripDeviceSuffix, isGroupChat } from '../utils/jid.js';
import { getSenderName, shouldRespondInGroup } from '../utils/message.js';

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
    const stream = await sendMessageToAI(whatsappJid, text, {
      conversationType,
      senderJid: msg.key.participant,
      senderName: getSenderName(msg),
    });

    // Accumulate response chunks
    let response = '';
    for await (const chunk of stream) {
      response += chunk;
    }

    // Send complete response
    await sock.sendMessage(whatsappJid, { text: response });

    logger.info(
      { to: whatsappJid, responseLength: response.length },
      'Sent AI response'
    );
  } catch (error) {
    logger.error({ error, whatsappJid }, 'Error processing message');
    await sock.sendMessage(whatsappJid, {
      text: 'Sorry, I encountered an error processing your message. Please try again.',
    });
  } finally {
    await sock.sendPresenceUpdate('paused', whatsappJid);
  }
}
