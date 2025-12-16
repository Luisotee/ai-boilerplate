import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import qrcode from "qrcode-terminal";
import { logger } from "./logger.js";
import { setBaileysSocket } from "./services/baileys.js";

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

export async function initializeWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

  const sock = makeWASocket({
    auth: state,
    logger: logger.child({ module: 'baileys' }),
  });

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      qrcode.generate(qr, { small: true });
      logger.info("QR Code displayed above. Scan with WhatsApp mobile app.");
    }

    if (connection === "close") {
      const shouldReconnect =
        (lastDisconnect?.error as Boom)?.output?.statusCode !==
        DisconnectReason.loggedOut;

      logger.info({ shouldReconnect }, "Connection closed");

      if (shouldReconnect) {
        initializeWhatsApp();
      }
    } else if (connection === "open") {
      logger.info("WhatsApp connection opened successfully");
      setBaileysSocket(sock); // Make socket available to API
    }
  });

  sock.ev.on("creds.update", saveCreds);

  // Simple message handler - just log for now
  sock.ev.on("messages.upsert", async ({ messages }) => {
    for (const msg of messages) {
      if (msg.key.fromMe || msg.key.remoteJid === "status@broadcast") continue;

      const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text;
      if (text) {
        logger.info({ from: msg.key.remoteJid, text }, "Received message");
        // Future: Forward to AI API or queue system
      }
    }
  });
}

