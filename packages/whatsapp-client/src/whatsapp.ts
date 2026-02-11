import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  normalizeMessageContent,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import { logger } from './logger.js';
import { config } from './config.js';
import { setBaileysSocket } from './services/baileys.js';
import { handleTextMessage } from './handlers/text.js';
import { transcribeAudioMessage } from './handlers/audio.js';
import { extractImageData } from './handlers/image.js';
import { extractDocumentData } from './handlers/document.js';
import { sendFailureReaction } from './utils/reactions.js';
import { stripDeviceSuffix, isGroupChat } from './utils/jid.js';
import { shouldRespondInGroup } from './utils/message.js';

const DEFAULT_IMAGE_PROMPT = 'Please describe and analyze this image';
const DEFAULT_DOCUMENT_PROMPT = 'I have uploaded a document for you to analyze';

// Reconnection state
let reconnectionAttempts = 0;
let reconnectionTimer: NodeJS.Timeout | null = null;
let isReconnecting = false;

/**
 * Calculate exponential backoff delay with jitter
 */
function calculateBackoffDelay(attempt: number): number {
  const { initialDelayMs, maxDelayMs, jitterMs } = config.reconnection;

  // Exponential backoff: initial * 2^attempt
  const exponentialDelay = initialDelayMs * Math.pow(2, attempt);

  // Cap at maximum delay
  const cappedDelay = Math.min(exponentialDelay, maxDelayMs);

  // Add random jitter to prevent thundering herd
  const jitter = Math.random() * jitterMs;

  return cappedDelay + jitter;
}

/**
 * Reset reconnection state after successful connection
 */
function resetReconnectionState(): void {
  reconnectionAttempts = 0;
  isReconnecting = false;
  if (reconnectionTimer) {
    clearTimeout(reconnectionTimer);
    reconnectionTimer = null;
  }
}

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

      logger.info(
        {
          shouldReconnect,
          reconnectionAttempts,
          statusCode: (lastDisconnect?.error as Boom)?.output?.statusCode,
        },
        'Connection closed'
      );

      if (shouldReconnect) {
        // Check if max attempts exceeded
        if (reconnectionAttempts >= config.reconnection.maxAttempts) {
          logger.error(
            {
              attempts: reconnectionAttempts,
              maxAttempts: config.reconnection.maxAttempts,
            },
            '❌ Max reconnection attempts exceeded. Manual restart required.'
          );
          return;
        }

        // Prevent multiple concurrent reconnection attempts
        if (isReconnecting) {
          logger.debug('Reconnection already in progress, skipping');
          return;
        }

        isReconnecting = true;
        reconnectionAttempts++;

        // Calculate backoff delay
        const delayMs = calculateBackoffDelay(reconnectionAttempts - 1);

        logger.info(
          {
            attempt: reconnectionAttempts,
            maxAttempts: config.reconnection.maxAttempts,
            delayMs: Math.round(delayMs),
          },
          `⏳ Reconnecting in ${Math.round(delayMs / 1000)}s...`
        );

        // Schedule reconnection with exponential backoff
        reconnectionTimer = setTimeout(() => {
          logger.info({ attempt: reconnectionAttempts }, 'Attempting reconnection');
          isReconnecting = false;
          initializeWhatsApp();
        }, delayMs);
      } else {
        logger.info('Logged out. QR code required for reconnection.');
        resetReconnectionState();
      }
    } else if (connection === 'open') {
      logger.info('✅ WhatsApp connection opened successfully');
      setBaileysSocket(sock);

      // Reset reconnection state on successful connection
      resetReconnectionState();
    }
  });

  sock.ev.on('creds.update', saveCreds);

  // Message handler
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const msg of messages) {
      try {
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

        // Determine if this is a group message and whether the bot should respond
        const whatsappJid = stripDeviceSuffix(msg.key.remoteJid!);
        const isGroup = isGroupChat(whatsappJid);
        const botJid = stripDeviceSuffix(sock.user!.id);
        const botLid = sock.user?.lid ? stripDeviceSuffix(sock.user.lid) : undefined;
        const saveOnly = isGroup && !shouldRespondInGroup(msg, botJid, botLid);

        // Get text from normalized message or transcribe audio
        let text = normalizedMessage?.conversation || normalizedMessage?.extendedTextMessage?.text;

        if (!text && normalizedMessage?.audioMessage) {
          text = await transcribeAudioMessage(sock, msg);
          if (!text) {
            if (!saveOnly) await sendFailureReaction(sock, msg);
            continue;
          }
        }

        // Handle image messages
        if (normalizedMessage?.imageMessage) {
          if (saveOnly) {
            // Save image context to history without downloading binary data
            const caption = normalizedMessage.imageMessage.caption;
            const marker = caption ? `[Image: ${caption}]` : '[Image]';
            await handleTextMessage(sock, msg, marker, undefined, undefined, { saveOnly: true });
          } else {
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
          }
          continue;
        }

        // Handle document messages (PDFs only)
        if (normalizedMessage?.documentMessage) {
          if (saveOnly) {
            // Save document context to history without downloading binary data
            const filename = normalizedMessage.documentMessage.fileName || 'unknown';
            const caption = normalizedMessage.documentMessage.caption;
            const marker = caption
              ? `[Document: ${filename}] - ${caption}`
              : `[Document: ${filename}]`;
            await handleTextMessage(sock, msg, marker, undefined, undefined, { saveOnly: true });
          } else {
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
          }
          continue;
        }

        if (text) {
          // Check group admin status for commands (only when needed)
          let isGroupAdmin: boolean | undefined;
          if (
            isGroup &&
            !saveOnly &&
            text
              .replace(/^(@\S+\s*)+/, '')
              .trimStart()
              .startsWith('/')
          ) {
            try {
              const metadata = await sock.groupMetadata(whatsappJid);
              const senderJid = stripDeviceSuffix(msg.key.participant || '');
              const participant = metadata.participants.find(
                (p) => stripDeviceSuffix(p.id) === senderJid
              );
              isGroupAdmin = participant?.admin === 'admin' || participant?.admin === 'superadmin';
              logger.debug({ senderJid, isGroupAdmin }, 'Checked group admin status for command');
            } catch (error) {
              logger.warn({ error, whatsappJid }, 'Failed to check group admin status');
            }
          }

          await handleTextMessage(
            sock,
            msg,
            text,
            undefined,
            undefined,
            saveOnly
              ? { saveOnly: true }
              : isGroupAdmin !== undefined
                ? { isGroupAdmin }
                : undefined
          );
        }
      } catch (error) {
        logger.error(
          {
            error,
            remoteJid: msg.key.remoteJid,
            messageId: msg.key.id,
            type,
          },
          'Fatal error processing message - continuing with next message'
        );

        // Attempt to send failure reaction
        try {
          await sendFailureReaction(sock, msg);
        } catch (reactionError) {
          logger.debug({ reactionError }, 'Failed to send error reaction');
        }

        // Continue to next message - DO NOT throw or break
      }
    }
  });
}
