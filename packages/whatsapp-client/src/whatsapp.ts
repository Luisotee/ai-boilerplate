import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  normalizeMessageContent,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import { logger } from './logger.js';
import { setBaileysSocket } from './services/baileys.js';
import { handleTextMessage } from './handlers/text.js';
import { transcribeAudioMessage } from './handlers/audio.js';
import { extractImageData } from './handlers/image.js';
import { extractDocumentData } from './handlers/document.js';
import { sendFailureReaction } from './utils/reactions.js';

const DEFAULT_IMAGE_PROMPT = 'Please describe and analyze this image';
const DEFAULT_DOCUMENT_PROMPT = 'I have uploaded a document for you to analyze';

export async function initializeWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');

  const sock = makeWASocket({
    auth: state,
    logger: logger.child({ module: 'baileys' }),
  });

  // Connection events
  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      qrcode.generate(qr, { small: true });
      logger.info('QR Code displayed above. Scan with WhatsApp mobile app.');
    }

    if (connection === 'close') {
      const shouldReconnect =
        (lastDisconnect?.error as Boom)?.output?.statusCode !== DisconnectReason.loggedOut;

      logger.info({ shouldReconnect }, 'Connection closed');

      if (shouldReconnect) {
        initializeWhatsApp();
      }
    } else if (connection === 'open') {
      logger.info('WhatsApp connection opened successfully');
      setBaileysSocket(sock);
    }
  });

  sock.ev.on('creds.update', saveCreds);

  // Message handler
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const msg of messages) {
      // Debug: log all incoming messages
      logger.debug(
        {
          remoteJid: msg.key.remoteJid,
          fromMe: msg.key.fromMe,
          type,
          messageKeys: msg.message ? Object.keys(msg.message) : [],
        },
        'Incoming message'
      );

      if (msg.key.fromMe || msg.key.remoteJid === 'status@broadcast') continue;

      // Normalize message content to handle wrappers (viewOnce, ephemeral, etc.)
      const normalizedMessage = normalizeMessageContent(msg.message);

      // Get text from normalized message or transcribe audio
      let text = normalizedMessage?.conversation || normalizedMessage?.extendedTextMessage?.text;

      if (!text && normalizedMessage?.audioMessage) {
        text = await transcribeAudioMessage(sock, msg);
        if (!text) {
          await sendFailureReaction(sock, msg);
          continue;
        }
      }

      // Handle image messages
      if (normalizedMessage?.imageMessage) {
        const imageData = await extractImageData(sock, msg);
        if (!imageData) {
          await sendFailureReaction(sock, msg);
          continue;
        }

        // Use caption if present, otherwise use default prompt
        const prompt = imageData.caption || DEFAULT_IMAGE_PROMPT;

        await handleTextMessage(sock, msg, prompt, {
          buffer: imageData.buffer,
          mimetype: imageData.mimetype,
        });
        continue;
      }

      // Handle document messages (PDFs only)
      if (normalizedMessage?.documentMessage) {
        const documentData = await extractDocumentData(sock, msg);
        if (!documentData) {
          await sendFailureReaction(sock, msg);
          continue;
        }

        // Only accept PDF documents for now
        if (documentData.mimetype !== 'application/pdf') {
          logger.info(
            { mimetype: documentData.mimetype },
            'Unsupported document type, only PDFs are supported'
          );
          await sock.sendMessage(msg.key.remoteJid!, {
            text: 'Sorry, I can only process PDF documents at the moment.',
          });
          continue;
        }

        // Use caption if present, otherwise use default prompt
        const prompt = documentData.caption || DEFAULT_DOCUMENT_PROMPT;

        await handleTextMessage(sock, msg, prompt, undefined, {
          buffer: documentData.buffer,
          mimetype: documentData.mimetype,
          filename: documentData.filename,
        });
        continue;
      }

      if (text) {
        await handleTextMessage(sock, msg, text);
      }
    }
  });
}
