import makeWASocket, {
  type WAMessage,
  type WASocket,
  DisconnectReason,
  normalizeMessageContent,
} from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import { readdir, rm } from 'node:fs/promises';
import { logger } from './logger.js';
import { Sentry } from './instrument.js';
import { config } from './config.js';
import {
  setBaileysSocket,
  setConnectionStatus,
  setLatestQr,
  setPairedPhoneNumber,
  getLiveSocket,
  setSocketOpen,
  isBaileysReady,
  clearBaileysSocket,
  sendSerialized,
  noteInbound,
  setLastDisconnect,
  setReconnectAttempts,
} from './services/baileys.js';
import { getWaVersionConfig } from './services/wa-version.js';
import { flushPendingCredsSave, useAtomicMultiFileAuthState } from './services/auth-state.js';
import {
  clearGroupCache,
  invalidateGroup,
  isCompleteGroupMetadata,
  primeGroup,
} from './services/group-cache.js';
import { groupHasWhitelistedMember } from './services/groups.js';
import { resolveSenderGroupAdmin } from './services/group-admin.js';
import { handleTextMessage } from './handlers/text.js';
import { transcribeAudioMessage } from './handlers/audio.js';
import { extractImageData } from './handlers/image.js';
import { extractDocumentData } from './handlers/document.js';
import { sendFailureReaction } from './utils/reactions.js';
import {
  stripDeviceSuffix,
  isGroupChat,
  isLid,
  resolveParticipantJid,
  resolveSenderPhone,
  extractPairedNumberFromCreds,
} from './utils/jid.js';
import { isWhitelisted } from './utils/whitelist.js';
import { gateMessage, type Gate } from './utils/gating.js';
import { shouldRespondInGroup } from './utils/message.js';

const DEFAULT_IMAGE_PROMPT = 'Please describe and analyze this image';
const DEFAULT_DOCUMENT_PROMPT = 'I have uploaded a document for you to analyze';

const AUTH_DIR = 'auth_info_baileys';

/**
 * The whitelist gate for one incoming message (see utils/gating.ts).
 *
 * `jid` mode (default) is exactly the historical check — the conversation's own
 * jid/phone against the whitelist — expressed through the shared truth table:
 * a group is in scope only when listed, and then every member may address the
 * bot. `membership` mode additionally admits a group that has a whitelisted
 * member, but only a whitelisted SENDER may trigger a reply there.
 *
 * Resolves nothing it does not need: the participant identity and the
 * membership lookup run only for a group in `membership` mode, and the lookup
 * is skipped when the group is listed or the sender is whitelisted.
 */
async function resolveGate(
  sock: WASocket,
  msg: WAMessage,
  whatsappJid: string,
  isGroup: boolean,
  phone: string | undefined
): Promise<Gate> {
  const wl = config.whitelistPhones;
  const whitelistEnabled = wl.size > 0;
  if (!whitelistEnabled) {
    return gateMessage({
      isGroup,
      whitelistEnabled,
      senderWhitelisted: true,
      groupExplicit: false,
      groupAllowed: true,
    });
  }

  const chatListed = isWhitelisted(wl, whatsappJid, phone);
  if (!isGroup || config.groupGating === 'jid') {
    return gateMessage({
      isGroup,
      whitelistEnabled,
      senderWhitelisted: chatListed,
      groupExplicit: chatListed,
      groupAllowed: chatListed,
    });
  }

  // membership mode, group chat. The sender is the participant — under v7 LID
  // addressing resolved to a phone JID where possible, and the raw (possibly
  // LID) participant is checked too so a verbatim `…@lid` entry still works.
  const participant = msg.key.participant ? stripDeviceSuffix(msg.key.participant) : undefined;
  const participantJid = await resolveParticipantJid(
    sock,
    msg.key.participant,
    msg.key.participantAlt
  );
  const senderWhitelisted =
    (participantJid !== undefined && isWhitelisted(wl, participantJid)) ||
    (participant !== undefined && isWhitelisted(wl, participant));
  const groupAllowed =
    chatListed || senderWhitelisted || (await groupHasWhitelistedMember(sock, whatsappJid, wl));

  return gateMessage({
    isGroup,
    whitelistEnabled,
    senderWhitelisted,
    groupExplicit: chatListed,
    groupAllowed,
  });
}

/** Delete the stored Baileys creds so the next init drops back into QR (unregistered) mode.
 *  Clears the directory contents — not the dir itself, which is the session volume mountpoint. */
async function clearAuthState(): Promise<void> {
  setPairedPhoneNumber(null);
  // A creds write still in flight would land after the wipe and resurrect them.
  await flushPendingCredsSave(3000);
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
 * real outcome. First bumps the socket generation (`socketEpoch`) so any in-flight
 * events from the current socket — notably a `creds.update` that would otherwise
 * re-persist the creds we're about to wipe — are ignored by the guarded handlers.
 * Then reaches the socket via `getLiveSocket()` (which sees a socket created but not
 * yet `open`ed, i.e. the initial-pairing window), detaches its listeners, unlinks
 * via `sock.logout()` when open (or just ends it when pre-open), and drops it. Either
 * way the on-disk creds are wiped and the client re-initialises so a fresh pairing QR
 * is issued; a failure to clear creds or re-init throws and surfaces to the caller.
 *
 * The epoch guard + tracking-at-creation close the pre-open unlink race (issue #4):
 * an unlink during initial pairing now tears the pending socket down instead of
 * letting it resurrect the just-wiped auth state.
 */
export async function logoutWhatsApp(): Promise<void> {
  logger.info('Forcing WhatsApp logout / re-pair');

  // Claim a new generation up front so the current socket's in-flight handlers go inert
  // (belt-and-braces alongside the removeAllListeners below).
  socketEpoch++;

  // getLiveSocket() reaches a socket that is created but not yet `open` (the pairing
  // window), so an unlink there tears it down instead of orphaning it.
  const sock = getLiveSocket();
  if (sock) {
    const wasOpen = isBaileysReady();
    sock.ev.removeAllListeners('connection.update');
    sock.ev.removeAllListeners('creds.update');
    if (wasOpen) {
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
    } else {
      // A not-yet-open socket has nothing registered server-side to unlink, and
      // logout() on it can hang the WS — just end it locally.
      try {
        sock.end(undefined);
      } catch (endErr) {
        logger.trace({ err: endErr }, 'socket.end() failed (already dead)');
      }
    }
    clearBaileysSocket();
    clearGroupCache();
  }

  resetReconnectionState();
  setConnectionStatus('connecting'); // don't serve a stale 'connected' between wipe and re-open
  setLatestQr(null);
  await clearAuthState(); // throws on FS failure → route 500 → dashboard 503
  await initializeWhatsApp(); // awaited: single re-init owner, errors surface
}

/**
 * Stop the WhatsApp side for a process shutdown: no more reconnects, end the
 * socket, and wait (bounded) for an in-flight creds write so SIGTERM can't
 * leave a half-written creds.json. Not epoch-bumped on purpose: the current
 * socket's last `creds.update` must still persist.
 */
export async function shutdownWhatsApp(): Promise<void> {
  shuttingDown = true;
  resetReconnectionState();
  const sock = getLiveSocket();
  if (sock) {
    // rc13's end() is async and awaits ws.close(), which can hang: don't await it.
    void Promise.resolve()
      .then(() => sock.end(undefined))
      .catch((err: unknown) => logger.debug({ err }, 'socket.end() failed during shutdown'));
  }
  if (!(await flushPendingCredsSave(3000))) {
    logger.warn('A WhatsApp creds write was still running at shutdown');
  }
}

// Monotonic socket generation. Bumped on every init and on logout; the connection
// and creds handlers capture their generation and go inert once it's superseded, so a
// stale socket can't clobber a newer one or re-persist auth state we've since wiped.
let socketEpoch = 0;

// Set by shutdownWhatsApp(): the socket's close must not schedule a reconnect.
let shuttingDown = false;

// Reconnection state
let reconnectionAttempts = 0;
let reconnectionTimer: NodeJS.Timeout | null = null;
let isReconnecting = false;
// Armed on 'open'; resets the attempt counter only once the link has stayed up
// for `config.reconnection.stableMs`, so an open→close flap keeps backing off.
let stableTimer: NodeJS.Timeout | null = null;

/**
 * Calculate exponential backoff delay with jitter. Once `maxAttempts` failures
 * in a row are reached, retry at the long interval instead of `maxDelayMs`:
 * ~120 connects/hour on a persistent failure is how accounts get restricted.
 */
function calculateBackoffDelay(attempt: number): number {
  const { initialDelayMs, maxDelayMs, jitterMs, maxAttempts, longDelayMs } = config.reconnection;

  // Exponential backoff: initial * 2^attempt, capped; long interval past maxAttempts
  const baseDelay =
    attempt >= maxAttempts
      ? longDelayMs
      : Math.min(initialDelayMs * Math.pow(2, attempt), maxDelayMs);

  // Add random jitter to prevent thundering herd
  const jitter = Math.random() * jitterMs;

  return baseDelay + jitter;
}

// Transport watchdog. rc13 already ends a socket after ~35s without a frame
// (keepalive), but its end() awaits ws.close() before emitting 'close'; if that
// hangs we never reconnect. This backstop tears the socket down itself after
// 5 min with no WebSocket frame. Deliberately NOT an app-level "no messages"
// watchdog: quiet accounts would flap.
const TRANSPORT_WATCHDOG_MS = 5 * 60_000;
const WATCHDOG_CHECK_MS = 60_000;
let watchdogTimer: NodeJS.Timeout | null = null;

function stopWatchdog(): void {
  if (watchdogTimer) {
    clearInterval(watchdogTimer);
    watchdogTimer = null;
  }
}

/** Drop a socket that stopped talking and reconnect, without waiting for its 'close'. */
function forceReconnect(sock: WASocket, reason: string): void {
  socketEpoch++; // the old socket's late events go inert
  sock.ev.removeAllListeners('connection.update');
  sock.ev.removeAllListeners('creds.update');
  void Promise.resolve()
    .then(() => sock.end(undefined))
    .catch((err: unknown) => logger.trace({ err }, 'socket.end() failed (already dead)'));
  clearBaileysSocket();
  clearStableTimer();
  setConnectionStatus('disconnected');
  setLastDisconnect(null, reason);
  scheduleReconnect();
}

function clearStableTimer(): void {
  if (stableTimer) {
    clearTimeout(stableTimer);
    stableTimer = null;
  }
}

/**
 * Reset reconnection state (logout / re-pair paths)
 */
function resetReconnectionState(): void {
  reconnectionAttempts = 0;
  setReconnectAttempts(0);
  isReconnecting = false;
  clearStableTimer();
  stopWatchdog();
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
function scheduleReconnect(delayOverrideMs?: number): void {
  if (shuttingDown) return;

  // Check if max attempts exceeded
  if (reconnectionAttempts >= config.reconnection.maxAttempts) {
    // Don't give up — keep retrying at the long interval so a prolonged outage
    // self-heals when connectivity returns. Clamp the counter so it doesn't grow
    // unbounded.
    reconnectionAttempts = config.reconnection.maxAttempts;
    logger.warn(
      {
        attempts: reconnectionAttempts,
        maxAttempts: config.reconnection.maxAttempts,
        longDelayMs: config.reconnection.longDelayMs,
      },
      'Max reconnection attempts reached — continuing to retry at the long interval.'
    );
  }

  // Prevent multiple concurrent reconnection attempts
  if (isReconnecting) {
    logger.debug('Reconnection already in progress, skipping');
    return;
  }

  isReconnecting = true;
  reconnectionAttempts++;
  setReconnectAttempts(reconnectionAttempts);

  // Calculate backoff delay (a 440 conflict passes its own, longer one)
  const delayMs = delayOverrideMs ?? calculateBackoffDelay(reconnectionAttempts - 1);

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
  const myEpoch = ++socketEpoch; // this socket's generation; guards below ignore stale ones
  // Atomic creds.json writes (temp + fsync + rename, with a .bak restored at
  // startup): a truncated creds.json would otherwise force a re-pair.
  const { state, saveCreds } = await useAtomicMultiFileAuthState(AUTH_DIR);
  // Populate paired phone number from existing saved creds so it is known even before
  // the socket opens or while reconnecting.
  const savedPhone = extractPairedNumberFromCreds(state.creds);
  if (savedPhone) {
    setPairedPhoneNumber(savedPhone);
  }
  // Spread, never `version: versionConfig.version` — an explicit undefined key would
  // clobber Baileys' bundled default and crash the handshake. See services/wa-version.ts.
  const versionConfig = await getWaVersionConfig();

  const sock = makeWASocket({
    auth: state,
    ...versionConfig,
    logger: logger.child({ module: 'baileys' }),
    browser: ['AI Boilerplate', 'Chrome', '131.0.0'],
    // rc13 defaults to true, which keeps the number "online" and mutes push
    // notifications on the phone that owns it.
    markOnlineOnConnect: false,
  });
  // Track at creation (not on 'open') so teardown can reach a not-yet-open socket.
  setBaileysSocket(sock);

  // Connection events
  sock.ev.on('connection.update', async (update) => {
    if (myEpoch !== socketEpoch) return; // a newer socket has superseded this generation
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
      clearStableTimer(); // it didn't stay up long enough to count as healthy
      stopWatchdog();
      const disconnectError = lastDisconnect?.error as Boom | undefined;
      setLastDisconnect(
        disconnectError?.output?.statusCode ?? null,
        disconnectError?.message ?? null
      );
      if (shuttingDown) {
        logger.info('WhatsApp connection closed for shutdown');
        return;
      }
      const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      const isConflict = statusCode === DisconnectReason.connectionReplaced;

      if (statusCode === 405) {
        logger.error(
          { statusCode },
          'WhatsApp rejected the connection (405) — usually an outdated WA Web version. ' +
            'Retrying; if this persists, the fetched version may be stale.'
        );
      }

      logger.info({ shouldReconnect, reconnectionAttempts, statusCode }, 'Connection closed');

      if (isConflict) {
        // 440: another client opened this session. Reconnecting at once just
        // kicks it (or us) again, in a loop WhatsApp may punish; stopping for
        // good needs a manual restart. Wait a long cooldown instead. The status
        // stays 'disconnected' (a closed enum for the dashboard); /health shows
        // the 440 in lastDisconnect.
        logger.error(
          { statusCode, delayMs: config.reconnection.conflictDelayMs },
          'WhatsApp session conflict (440): another client took over this session — ' +
            'waiting before reconnecting.'
        );
        Sentry.captureMessage('WhatsApp session conflict (440)', 'warning');
        scheduleReconnect(config.reconnection.conflictDelayMs);
      } else if (shouldReconnect) {
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
      setSocketOpen(true); // socket already tracked at creation; just mark the link open
      setConnectionStatus('connected');
      setLatestQr(null); // clear the pairing QR once linked
      const phone = extractPairedNumberFromCreds(state.creds);
      if (phone) {
        setPairedPhoneNumber(phone);
      }

      // Don't reset the attempt counter yet: only once the link has stayed up
      // for stableMs, so a socket that opens and drops again keeps backing off.
      isReconnecting = false;
      if (reconnectionTimer) {
        clearTimeout(reconnectionTimer);
        reconnectionTimer = null;
      }
      stopWatchdog();
      let lastFrameAt = Date.now();
      sock.ws?.on('frame', () => {
        lastFrameAt = Date.now();
      });
      watchdogTimer = setInterval(() => {
        if (myEpoch !== socketEpoch || shuttingDown) {
          stopWatchdog();
          return;
        }
        const silentMs = Date.now() - lastFrameAt;
        if (silentMs < TRANSPORT_WATCHDOG_MS) return;
        logger.error(
          { silentMs },
          'No WebSocket frame from WhatsApp for 5 min while connected — forcing a reconnect'
        );
        stopWatchdog();
        forceReconnect(sock, 'transport_watchdog');
      }, WATCHDOG_CHECK_MS);

      clearStableTimer();
      stableTimer = setTimeout(() => {
        stableTimer = null;
        if (myEpoch !== socketEpoch) return;
        reconnectionAttempts = 0;
        setReconnectAttempts(0);
        logger.debug('WhatsApp connection stable — reconnection backoff reset');
      }, config.reconnection.stableMs);
    }
  });

  sock.ev.on('creds.update', async () => {
    if (myEpoch !== socketEpoch) return; // stale socket must not re-persist wiped creds
    try {
      await saveCreds();
    } catch (err) {
      // An async listener's rejection would be an unhandledRejection (exit 1).
      logger.error({ err }, 'Failed to save WhatsApp creds');
    }
    const phone = extractPairedNumberFromCreds(state.creds);
    if (phone) {
      setPairedPhoneNumber(phone);
    }
  });

  // A renamed group must not keep serving its old subject as the conversation
  // name. Deliberately NOT epoch-guarded, unlike the handlers above: those
  // gate side effects that must not happen twice (persisting creds, serving a
  // QR), whereas this is an idempotent cache delete whose worst outcome is one
  // refetch. Reconnect is exactly when buffered events flush, so dropping them
  // on a superseded generation would pin a stale name for the full TTL.
  //
  // Baileys also emits this event from inside `groupFetchAllParticipating()`
  // with COMPLETE metadata for every group. That burst is fresh data, not a
  // change: evicting on it would drop the very snapshot it belongs to (so the
  // shared-groups lookup would refetch the whole fleet every time). Complete
  // entries prime the cache instead — epoch-guarded, since a superseded socket
  // may belong to a previous account.
  sock.ev.on('groups.update', (updates) => {
    for (const update of updates) {
      if (!update.id) continue;
      if (isCompleteGroupMetadata(update)) {
        if (myEpoch === socketEpoch) primeGroup(update);
      } else {
        invalidateGroup(update.id);
      }
    }
  });

  sock.ev.on('group-participants.update', ({ id }) => {
    invalidateGroup(id);
  });

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
        noteInbound();
        if (!msg.key.remoteJid) continue;

        // Extract phone number and LID for user identity resolution.
        // A LID-addressed chat carries no phone in its JID, so recover it from
        // the message envelope (key.remoteJidAlt), falling back to Baileys'
        // LID↔PN mapping store. Groups resolve to undefined — correct, since
        // the User row *is* the group and has no phone of its own.
        const whatsappJid = stripDeviceSuffix(msg.key.remoteJid!);
        const isGroup = isGroupChat(whatsappJid);
        const whatsappLid = isLid(whatsappJid) ? whatsappJid : undefined;
        const phone = await resolveSenderPhone(sock, msg.key.remoteJid!, msg.key.remoteJidAlt);

        // Whitelist gate. Must run AFTER resolveSenderPhone — a LID-addressed
        // chat's JID digits are an anonymized account id, not a phone, so a
        // bare-phone whitelist entry can only match via the resolved E.164.
        // Must run BEFORE `sock.user!.id` below: if sock.user is unset that
        // throws into the per-message catch, which reacts to the message —
        // sending an outbound reaction into a chat we just blocked. It also
        // stays ahead of every expensive step (transcription, media download).
        // An unresolvable phone fails closed; whitelist the raw `@lid` if a
        // contact is ever stuck behind a cold LID↔PN mapping.
        // (GROUP_GATING=membership may fetch the group's metadata here, but
        // only for a group with a non-whitelisted sender that is not listed.)
        const gate = await resolveGate(sock, msg, whatsappJid, isGroup, phone);
        if (gate.skip) {
          logger.info(
            {
              remoteJid: msg.key.remoteJid,
              whatsappJid,
              isGroup,
              groupGating: config.groupGating,
              // Only meaningful for a private chat: a group has no phone of its
              // own, so logging phoneResolved:false there would read as a
              // resolution failure and send the operator after the wrong fix.
              ...(isGroup ? {} : { phoneResolved: Boolean(phone) }),
            },
            'Skipping non-whitelisted chat'
          );
          continue;
        }

        // Normalize message content to handle wrappers (viewOnce, ephemeral, etc.)
        const normalizedMessage = normalizeMessageContent(msg.message);

        // Determine whether the bot should respond in a group
        const botJid = stripDeviceSuffix(sock.user!.id);
        const botLid = sock.user?.lid ? stripDeviceSuffix(sock.user.lid) : undefined;
        // A kept group message is save-only when it isn't addressed to the bot,
        // or (GROUP_GATING=membership) its sender may not trigger a reply.
        const saveOnly =
          isGroup && (gate.suppressResponse || !shouldRespondInGroup(msg, botJid, botLid));

        // Get text from normalized message or transcribe audio
        let text = normalizedMessage?.conversation || normalizedMessage?.extendedTextMessage?.text;

        if (!text && normalizedMessage?.audioMessage) {
          text = await transcribeAudioMessage(sock, msg);
          if (!text) {
            if (!saveOnly) await sendFailureReaction(msg);
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
              await sendFailureReaction(msg);
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
              await sendFailureReaction(msg);
              continue;
            }

            // Only accept PDF documents for now
            if (documentData.mimetype !== 'application/pdf') {
              logger.info(
                { mimetype: documentData.mimetype },
                'Unsupported document type, only PDFs are supported'
              );
              await sendSerialized(msg.key.remoteJid!, {
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
          // Admin status for addressed group messages: admin commands and
          // group-setting agent tools both fail closed without it. Saved-only
          // chatter never pays for the lookup.
          const isGroupAdmin =
            isGroup && !saveOnly
              ? await resolveSenderGroupAdmin(sock, whatsappJid, msg.key, text)
              : undefined;

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
          await sendFailureReaction(msg);
        } catch (reactionError) {
          logger.debug({ reactionError }, 'Failed to send error reaction');
        }

        // Continue to next message - DO NOT throw or break
      }
    }
  });
}
