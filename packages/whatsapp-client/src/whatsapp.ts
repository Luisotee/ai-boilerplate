import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import qrcode from "qrcode-terminal";
import { logger } from "./logger.js";
import { sendMessageToAI } from "./api-client.js";
import { QueuedMessage, MessageQueue } from "./types.js";

// Reaction status emojis
const REACTION_QUEUED = '⏳';      // Message received, queued for processing
const REACTION_PROCESSING = '🤖';  // AI is generating response
const REACTION_DONE = '✅';        // Response sent successfully
const REACTION_ERROR = '❌';       // Error occurred during processing

// Store bot's JID globally
let botJid: string | null = null;

// Per-conversation message queues
const conversationQueues = new Map<string, MessageQueue>();

// Helper: Normalize JID by removing device suffix
function normalizeJid(jid: string): string {
  // Remove device ID suffix like :50 or :XX
  // Example: "5491126726818:50@s.whatsapp.net" -> "5491126726818@s.whatsapp.net"
  return jid.replace(/:\d+@/, '@');
}

// Helper: Check if JID is a group
function isGroupChat(jid: string): boolean {
  return jid.endsWith("@g.us");
}

// Helper: Extract phone number from JID
function extractPhoneFromJid(jid: string): string {
  return jid.split("@")[0];
}

// Helper: Get sender name from message
function getSenderName(msg: WAMessage): string {
  return (
    msg.pushName ||
    msg.verifiedBizName ||
    extractPhoneFromJid(msg.key.participant || msg.key.remoteJid!)
  );
}

// Helper: Check if bot is mentioned
function isBotMentioned(msg: WAMessage, botJid: string): boolean {
  const mentionedJids = msg.message?.extendedTextMessage?.contextInfo?.mentionedJid || [];
  const matches = mentionedJids.includes(botJid);
  logger.debug({
    botJid,
    mentionedJids,
    matches
  }, "Checking bot mention");
  return matches;
}

// Helper: Check if message is a reply to bot
function isReplyToBotMessage(msg: WAMessage, botJid: string): boolean {
  const quotedParticipant = msg.message?.extendedTextMessage?.contextInfo?.participant;
  return quotedParticipant === botJid;
}

// Helper: Should bot respond in group
function shouldRespondInGroup(msg: WAMessage, botJid: string): boolean {
  return isBotMentioned(msg, botJid) || isReplyToBotMessage(msg, botJid);
}

/**
 * Send reaction to a message
 * @param sock - Baileys socket instance
 * @param messageKey - The message key to react to
 * @param emoji - Emoji to react with (or empty string to remove)
 */
async function sendReaction(
  sock: any,
  messageKey: any,
  emoji: string
): Promise<void> {
  try {
    await sock.sendMessage(messageKey.remoteJid, {
      react: {
        text: emoji,
        key: messageKey
      }
    });
    logger.debug({
      jid: messageKey.remoteJid,
      messageId: messageKey.id,
      emoji
    }, "Sent reaction");
  } catch (error) {
    // Log but don't throw - reactions are non-critical
    logger.warn({
      error,
      jid: messageKey.remoteJid,
      emoji
    }, "Failed to send reaction");
  }
}

/**
 * Enqueue a message for processing
 * Messages are processed in FIFO order per conversation
 */
function enqueueMessage(sock: any, queuedMessage: QueuedMessage): void {
  const { whatsappJid } = queuedMessage;

  // Get or create queue for this conversation
  if (!conversationQueues.has(whatsappJid)) {
    conversationQueues.set(whatsappJid, {
      messages: [],
      isProcessing: false
    });
  }

  const queue = conversationQueues.get(whatsappJid)!;
  queue.messages.push(queuedMessage);

  logger.info({
    whatsappJid,
    queueLength: queue.messages.length,
    isProcessing: queue.isProcessing
  }, "Message enqueued");

  // Start processing if not already running
  if (!queue.isProcessing) {
    processQueue(sock, whatsappJid).catch(err => {
      logger.error({ error: err, whatsappJid }, "Queue processing failed");
    });
  }
}

/**
 * Process all messages in a conversation's queue sequentially
 * Ensures messages are handled in order with proper error handling
 */
async function processQueue(sock: any, whatsappJid: string): Promise<void> {
  const queue = conversationQueues.get(whatsappJid);
  if (!queue) return;

  // Prevent concurrent processing of same queue
  if (queue.isProcessing) {
    logger.debug({ whatsappJid }, "Queue already processing");
    return;
  }

  queue.isProcessing = true;

  try {
    while (queue.messages.length > 0) {
      const queuedMessage = queue.messages.shift()!;

      logger.info({
        whatsappJid,
        remainingInQueue: queue.messages.length
      }, "Processing queued message");

      // Update reaction from ⏳ (queued) to 🤖 (processing)
      await sendReaction(sock, queuedMessage.messageKey, REACTION_PROCESSING);

      try {
        // Process the message
        await processMessage(sock, queuedMessage);

        // Success - send done reaction
        await sendReaction(sock, queuedMessage.messageKey, REACTION_DONE);

        logger.info({
          whatsappJid,
          messageText: queuedMessage.messageText
        }, "Message processed successfully");

      } catch (error) {
        logger.error({
          error,
          whatsappJid,
          messageText: queuedMessage.messageText
        }, "Error processing message");

        // Error reaction
        await sendReaction(sock, queuedMessage.messageKey, REACTION_ERROR);

        // Send error message to user
        await sock.sendMessage(whatsappJid, {
          text: "Sorry, I encountered an error. Please try again."
        });
      }
    }
  } finally {
    queue.isProcessing = false;

    // Cleanup empty queues to prevent memory leaks
    if (queue.messages.length === 0) {
      conversationQueues.delete(whatsappJid);
      logger.debug({ whatsappJid }, "Queue cleaned up");
    }
  }
}

/**
 * Process a single message (contains the actual business logic)
 * Separated from queue management for clarity
 */
async function processMessage(sock: any, queuedMessage: QueuedMessage): Promise<void> {
  const { msg, messageText, whatsappJid, isGroup } = queuedMessage;

  if (isGroup) {
    if (!botJid) {
      logger.warn("Bot JID not yet available, skipping group message");
      throw new Error("Bot JID not available");
    }

    const senderJid = msg.key.participant!;
    const senderName = getSenderName(msg);
    const shouldRespond = shouldRespondInGroup(msg, botJid);

    if (!shouldRespond) {
      // Save message without responding
      await sendMessageToAI(whatsappJid, messageText, {
        senderJid,
        senderName,
        saveOnly: true,
      });
      logger.info({ whatsappJid, senderJid, senderName }, "Group message saved");
      return;
    }

    // Generate and send AI response
    const formattedMessage = `${senderName}: ${messageText}`;
    const stream = await sendMessageToAI(whatsappJid, formattedMessage, {
      senderJid,
      senderName,
    });

    await sendProgressiveMessage(sock, whatsappJid, stream);
    logger.info({ whatsappJid, senderName }, "Sent AI response to group");

  } else {
    // Handle private message
    const stream = await sendMessageToAI(whatsappJid, messageText);
    await sendProgressiveMessage(sock, whatsappJid, stream);
    logger.info({ whatsappJid }, "Sent AI response");
  }
}

/**
 * Sends message with progressive updates as content streams in
 * @param sock - Baileys socket instance
 * @param jid - WhatsApp JID to send to
 * @param stream - Async iterable of response chunks
 * @returns Complete final response text
 */
async function sendProgressiveMessage(
  sock: any,
  jid: string,
  stream: AsyncIterable<string>
): Promise<string> {
  let fullResponse = "";
  let sentMessage: any = null;
  let editingEnabled = true;
  let lastUpdateLength = 0;
  const UPDATE_THRESHOLD_CHARS = 50;  // Update every 50 characters
  const EDIT_DELAY_MS = 500;          // Rate limit protection

  try {
    for await (const chunk of stream) {
      fullResponse += chunk;

      // Update when we've accumulated enough new characters
      const newCharsAccumulated = fullResponse.length - lastUpdateLength;
      if (newCharsAccumulated >= UPDATE_THRESHOLD_CHARS && editingEnabled) {
        if (!sentMessage) {
          // Send initial message
          try {
            sentMessage = await sock.sendMessage(jid, {
              text: fullResponse
            });
            logger.debug({ jid, length: fullResponse.length }, "Initial message sent");
            lastUpdateLength = fullResponse.length;
          } catch (error) {
            logger.error({ error, jid }, "Failed to send initial message");
            editingEnabled = false;
          }
        } else {
          // Edit existing message
          try {
            await sock.sendMessage(jid, {
              text: fullResponse,
              edit: sentMessage.key
            });
            logger.debug({ jid, length: fullResponse.length }, "Message updated");
            lastUpdateLength = fullResponse.length;

            // Rate limit protection: delay before next edit
            await new Promise(resolve => setTimeout(resolve, EDIT_DELAY_MS));
          } catch (error) {
            logger.warn({ error, jid }, "Failed to edit message, disabling progressive updates");
            editingEnabled = false; // Disable further edits but continue streaming
          }
        }
      }
    }

    // Send final complete message
    if (sentMessage && editingEnabled) {
      // Final edit with complete response
      try {
        await sock.sendMessage(jid, {
          text: fullResponse,
          edit: sentMessage.key
        });
        logger.info({ jid, length: fullResponse.length }, "Final message sent (edited)");
      } catch (error) {
        logger.error({ error, jid }, "Failed to send final edit");
        // Fallback: send as new message
        await sock.sendMessage(jid, { text: fullResponse });
        logger.info({ jid }, "Final message sent as fallback");
      }
    } else if (!sentMessage) {
      // No message sent yet, send complete response now
      await sock.sendMessage(jid, { text: fullResponse });
      logger.info({ jid, length: fullResponse.length }, "Final message sent (no edits)");
    }

    return fullResponse;
  } catch (error) {
    logger.error({ error, jid }, "Error in progressive message sending");

    // Ensure message is sent even if streaming fails
    if (fullResponse && !sentMessage) {
      await sock.sendMessage(jid, { text: fullResponse });
    }

    throw error;
  }
}

export async function startWhatsAppClient() {
  const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

  const sock = makeWASocket({
    auth: state,
    browser: ["AI Agent", "Chrome", "1.0.0"],
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    // Handle QR code display
    if (qr) {
      qrcode.generate(qr, { small: true });
      logger.info("QR Code displayed above. Scan with WhatsApp mobile app.");
    }

    if (connection === "close") {
      const shouldReconnect =
        (lastDisconnect?.error as Boom)?.output?.statusCode !==
        DisconnectReason.loggedOut;

      logger.info({ error: lastDisconnect?.error }, "Connection closed");

      if (shouldReconnect) {
        logger.info("Reconnecting...");
        startWhatsAppClient();
      } else {
        logger.info("Logged out. Please scan QR code again.");
      }
    } else if (connection === "open") {
      // Capture and normalize bot's JID
      const rawBotJid = sock.user?.id;
      botJid = rawBotJid ? normalizeJid(rawBotJid) : null;
      logger.info({ rawBotJid, normalizedBotJid: botJid }, "WhatsApp connection opened successfully");
    }
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("messages.upsert", async ({ messages }) => {
    for (const msg of messages) {
      await enqueueIncomingMessage(sock, msg);
    }
  });

  return sock;
}

/**
 * Entry point for incoming messages
 * Validates and queues messages for processing
 */
async function enqueueIncomingMessage(sock: any, msg: WAMessage): Promise<void> {
  // Ignore messages from self or broadcast
  if (msg.key.fromMe || msg.key.remoteJid === "status@broadcast") return;

  const messageText = msg.message?.conversation || msg.message?.extendedTextMessage?.text;
  if (!messageText || !msg.key.remoteJid) return;

  const whatsappJid = msg.key.remoteJid;
  const isGroup = isGroupChat(whatsappJid);

  logger.info({ whatsappJid, isGroup, message: messageText }, "Received message");

  // Send QUEUED reaction immediately
  await sendReaction(sock, msg.key, REACTION_QUEUED);

  // Create queued message object
  const queuedMessage: QueuedMessage = {
    msg,
    messageKey: msg.key,
    messageText,
    whatsappJid,
    isGroup
  };

  // Add to queue and start processing if needed
  enqueueMessage(sock, queuedMessage);
}

