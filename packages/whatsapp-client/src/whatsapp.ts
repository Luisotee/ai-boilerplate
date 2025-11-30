import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WAMessage,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import { logger } from './logger.js';
import { sendMessageToAI } from './api-client.js';

export async function startWhatsAppClient() {
  const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');

  const sock = makeWASocket({
    auth: state,
    printQRInTerminal: true,
    browser: ['AI Agent', 'Chrome', '1.0.0'],
  });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect } = update;

    if (connection === 'close') {
      const shouldReconnect =
        (lastDisconnect?.error as Boom)?.output?.statusCode !== DisconnectReason.loggedOut;

      logger.info({ error: lastDisconnect?.error }, 'Connection closed');

      if (shouldReconnect) {
        logger.info('Reconnecting...');
        startWhatsAppClient();
      } else {
        logger.info('Logged out. Please scan QR code again.');
      }
    } else if (connection === 'open') {
      logger.info('WhatsApp connection opened successfully');
    }
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const msg of messages) {
      await handleIncomingMessage(sock, msg);
    }
  });

  return sock;
}

async function handleIncomingMessage(sock: any, msg: WAMessage) {
  // Ignore messages from self or broadcast
  if (msg.key.fromMe || msg.key.remoteJid === 'status@broadcast') return;

  const messageText = msg.message?.conversation ||
                      msg.message?.extendedTextMessage?.text;

  if (!messageText || !msg.key.remoteJid) return;

  logger.info({
    from: msg.key.remoteJid,
    message: messageText,
  }, 'Received message');

  try {
    // Stream response from AI API
    const stream = await sendMessageToAI(msg.key.remoteJid, messageText);
    let fullResponse = '';

    for await (const chunk of stream) {
      fullResponse += chunk;
    }

    // Send complete response to WhatsApp
    await sock.sendMessage(msg.key.remoteJid, { text: fullResponse });

    logger.info({ to: msg.key.remoteJid }, 'Sent AI response');
  } catch (error) {
    logger.error({ error }, 'Error processing message');
    await sock.sendMessage(msg.key.remoteJid, {
      text: 'Sorry, I encountered an error. Please try again.',
    });
  }
}
