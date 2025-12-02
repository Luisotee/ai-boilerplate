import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import qrcode from "qrcode-terminal";
import { logger } from "./logger.js";
import { sendMessageToAI } from "./api-client.js";

// Store bot's JID globally
let botJid: string | null = null;

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
      await handleIncomingMessage(sock, msg);
    }
  });

  return sock;
}

async function handleIncomingMessage(sock: any, msg: WAMessage) {
  // Ignore messages from self or broadcast
  if (msg.key.fromMe || msg.key.remoteJid === "status@broadcast") return;

  const messageText = msg.message?.conversation || msg.message?.extendedTextMessage?.text;

  if (!messageText || !msg.key.remoteJid) return;

  const whatsappJid = msg.key.remoteJid;
  const isGroup = isGroupChat(whatsappJid);

  logger.info(
    {
      whatsappJid,
      isGroup,
      message: messageText,
    },
    "Received message"
  );

  // Handle group messages
  if (isGroup) {
    if (!botJid) {
      logger.warn("Bot JID not yet available, skipping group message");
      return;
    }

    const senderJid = msg.key.participant!;
    const senderName = getSenderName(msg);

    // Check if we should respond
    const shouldRespond = shouldRespondInGroup(msg, botJid);

    if (!shouldRespond) {
      // Save message without responding
      try {
        await sendMessageToAI(whatsappJid, messageText, {
          senderJid,
          senderName,
          saveOnly: true,
        });
        logger.info(
          { whatsappJid, senderJid, senderName },
          "Group message saved (not responding)"
        );
      } catch (error) {
        logger.error({ error }, "Error saving group message");
      }
      return;
    }

    // Respond to message
    try {
      const formattedMessage = `${senderName}: ${messageText}`;

      const stream = await sendMessageToAI(whatsappJid, formattedMessage, {
        senderJid,
        senderName,
      });

      let fullResponse = "";
      for await (const chunk of stream) {
        fullResponse += chunk;
      }

      await sock.sendMessage(whatsappJid, { text: fullResponse });
      logger.info({ whatsappJid, senderName }, "Sent AI response to group");
    } catch (error) {
      logger.error({ error }, "Error processing group message");
      await sock.sendMessage(whatsappJid, {
        text: "Sorry, I encountered an error. Please try again.",
      });
    }
  }
  // Handle private messages (existing logic unchanged)
  else {
    try {
      const stream = await sendMessageToAI(whatsappJid, messageText);
      let fullResponse = "";

      for await (const chunk of stream) {
        fullResponse += chunk;
      }

      await sock.sendMessage(whatsappJid, { text: fullResponse });
      logger.info({ whatsappJid }, "Sent AI response");
    } catch (error) {
      logger.error({ error }, "Error processing message");
      await sock.sendMessage(whatsappJid, {
        text: "Sorry, I encountered an error. Please try again.",
      });
    }
  }
}
