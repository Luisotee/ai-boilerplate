import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  normalizeMessageContent,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import { readdir, rm } from 'node:fs/promises';
import { logger } from './logger.js';
import { config } from './config.js';
import {
  setBaileysSocket,
  setConnectionStatus,
  setLatestQr,
  getBaileysSocket,
  isBaileysReady,
  clearBaileysSocket,
} from './services/baileys.js';
import { handleTextMessage } from './handlers/text.js';
import { transcribeAudioMessage } from './handlers/audio.js';
import { extractImageData } from './handlers/image.js';
import { extractDocumentData } from './handlers/document.js';
import { sendFailureReaction } from './utils/reactions.js';
import { stripDeviceSuffix, isGroupChat, phoneFromJid, isLid } from './utils/jid.js';
import { shouldRespondInGroup } from './utils/message.js';

const DEFAULT_IMAGE_PROMPT = 'Please describe and analyze this image';
const DEFAULT_DOCUMENT_PROMPT = 'I have uploaded a document for you to analyze';

const AUTH_DIR = 'auth_info_baileys';

/** Delete the stored Baileys creds so the next init drops back into QR (unregistered) mode.
 *  Clears the directory contents — not the dir itself, which is the session volume mountpoint. */
async function clearAuthState(): Promise<void> {
  try {
    const entries = await readdir(AUTH_DIR);
    await Promise.all(entries.map((e) => rm(`${AUTH_DIR}/${e}`, { recursive: true, force: true })));
    logger.info({ cleared: entries.length }, 'Cleared WhatsApp auth state');
  } catch (err) {
    logger.error({ err }, 'Failed to clear WhatsApp auth state');
    throw err;
  }
}

/**
 * Force a WhatsApp re-pair (used by the dashboard's "unlink" action).
 *
 * Owns the full teardown → clear → re-init so the caller's success reflects the
 * real outcome. When a device is linked, detach the socket's connection.update
 * listener (so its logout-triggered `close` can't race the re-init below), ask
 * WhatsApp to unlink via `sock.logout()`, then drop the stored socket. Either way
 * the on-disk creds are wiped and the client re-initialises so a fresh pairing QR
 * is issued; a failure to clear creds or re-init throws and surfaces to the caller.
 *
 * TODO(#4): a socket created but not yet `open`ed (initial pairing) isn't tracked
 * by the baileys singleton, so an unlink during that brief window can orphan it.
 */
export async function logoutWhatsApp(): Promise<void> {
  logger.info('Forcing WhatsApp logout / re-pair');

  if (isBaileysReady()) {
    const sock = getBaileysSocket();
    // Detach so the logout-triggered 'close' can't race the re-init below.
    sock.ev.removeAllListeners('connection.update');
    try {
      await sock.logout();
    } catch (err) {
      logger.warn({ err }, 'sock.logout() failed; ending the socket locally');
      try {
        sock.end(undefined);
      } catch (endErr) {
        logger.trace({ err: endErr }, 'socket.end() failed (already dead)');
      }
    }
    clearBaileysSocket();
  }

  resetReconnectionState();
  await clearAuthState(); // throws on FS failure → route 500 → dashboard 503
  await initializeWhatsApp(); // awaited: single re-init owner, errors surface
}

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

/**
 * Schedule a reconnection attempt with exponential backoff.
 *
 * Safe to call from the connection 'close' handler and from a failed re-init's
 * `.catch`: the `isReconnecting` guard prevents double-arming the timer, and
 * `reconnectionAttempts` is clamped at `maxAttempts` so a prolonged outage keeps
 * retrying at the capped interval rather than giving up. Crucially, a failed
 * attempt re-schedules itself — so an `initializeWhatsApp()` that throws *before*
 * a socket exists (which would otherwise emit no 'close' to re-arm anything) still
 * self-heals instead of stalling at disconnected/qr=null.
 */
function scheduleReconnect(): void {
  // Check if max attempts exceeded
  if (reconnectionAttempts >= config.reconnection.maxAttempts) {
    // Don't give up — keep retrying at the capped backoff so a prolonged outage
    // self-heals when connectivity returns. Clamp the counter so the delay stays
    // pinned at maxDelayMs instead of overflowing or growing unbounded.
    reconnectionAttempts = config.reconnection.maxAttempts;
    logger.warn(
      {
        attempts: reconnectionAttempts,
        maxAttempts: config.reconnection.maxAttempts,
      },
      'Max reconnection attempts reached — continuing to retry at the capped interval.'
    );
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
    initializeWhatsApp().catch((err) => {
      logger.error({ err }, 'Reconnection attempt failed');
      scheduleReconnect();
    });
  }, delayMs);
}

export async function initializeWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  const sock = makeWASocket({
    auth: state,
    logger: logger.child({ module: 'baileys' }),
    browser: ['AI Boilerplate', 'Chrome', '131.0.0'],
  });

  // Connection events
  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Retain the latest QR so it can be served over HTTP (the dashboard polls
      // for it); Baileys rotates it every ~20s while unpaired.
      setLatestQr(qr);
      setConnectionStatus('qr');
      qrcode.generate(qr, { small: true });
      logger.info('QR Code displayed above. Scan with WhatsApp mobile app.');
    }

    if (connection === 'close') {
      setConnectionStatus('disconnected');
      setLatestQr(null); // the socket that issued the QR is gone — don't serve a stale code
      clearBaileysSocket(); // the socket is dead — stop reporting "ready" until re-open
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
        scheduleReconnect();
      } else {
        // Logged out (e.g. the linked phone unlinked this device). The on-disk creds are
        // now invalid and Baileys won't emit a fresh QR while a registered cred set exists,
        // so wipe the auth state and re-initialise to drop back into QR mode automatically.
        logger.info(
          'Logged out — clearing stale credentials and re-initialising to issue a fresh QR.'
        );
        resetReconnectionState();
        try {
          await clearAuthState();
          await initializeWhatsApp();
        } catch (err) {
          // Don't stall at disconnected/qr=null — reschedule with backoff so a transient
          // FS/init failure self-heals instead of needing a manual restart.
          logger.error({ err }, 'Failed to re-initialise after logout — retrying with backoff');
          scheduleReconnect();
        }
      }
    } else if (connection === 'open') {
      logger.info('✅ WhatsApp connection opened successfully');
      setBaileysSocket(sock);
      setConnectionStatus('connected');
      setLatestQr(null); // clear the pairing QR once linked

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
        if (!msg.key.remoteJid) continue;

        // Whitelist check: skip non-whitelisted JIDs
        if (config.whitelistPhones.size > 0) {
          const remoteJid = msg.key.remoteJid!;
          const phone = remoteJid.replace(/@.*$/, '');
          if (!config.whitelistPhones.has(phone) && !config.whitelistPhones.has(remoteJid)) {
            logger.info({ remoteJid }, 'Skipping non-whitelisted JID');
            continue;
          }
        }

        // Normalize message content to handle wrappers (viewOnce, ephemeral, etc.)
        const normalizedMessage = normalizeMessageContent(msg.message);

        // Determine if this is a group message and whether the bot should respond
        const whatsappJid = stripDeviceSuffix(msg.key.remoteJid!);
        const isGroup = isGroupChat(whatsappJid);
        const botJid = stripDeviceSuffix(sock.user!.id);
        const botLid = sock.user?.lid ? stripDeviceSuffix(sock.user.lid) : undefined;
        const saveOnly = isGroup && !shouldRespondInGroup(msg, botJid, botLid);

        // Extract phone number and LID for user identity resolution
        const phone = phoneFromJid(whatsappJid) ?? undefined;
        const whatsappLid = isLid(whatsappJid) ? whatsappJid : undefined;

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
            await handleTextMessage(sock, msg, marker, undefined, undefined, {
              saveOnly: true,
              phone,
              whatsappLid,
            });
          } else {
            const imageData = await extractImageData(sock, msg);
            if (!imageData) {
              await sendFailureReaction(sock, msg);
              continue;
            }

            // Use caption if present, otherwise use default prompt
            const prompt = imageData.caption || DEFAULT_IMAGE_PROMPT;

            await handleTextMessage(
              sock,
              msg,
              prompt,
              {
                buffer: imageData.buffer,
                mimetype: imageData.mimetype,
              },
              undefined,
              { phone, whatsappLid }
            );
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
            await handleTextMessage(sock, msg, marker, undefined, undefined, {
              saveOnly: true,
              phone,
              whatsappLid,
            });
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

            await handleTextMessage(
              sock,
              msg,
              prompt,
              undefined,
              {
                buffer: documentData.buffer,
                mimetype: documentData.mimetype,
                filename: documentData.filename,
              },
              { phone, whatsappLid }
            );
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
            saveOnly ? { saveOnly: true, phone, whatsappLid } : { isGroupAdmin, phone, whatsappLid }
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
