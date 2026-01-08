import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  WAMessage,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import qrcode from "qrcode-terminal";
import { logger } from "./logger.js";
import { setBaileysSocket } from "./services/baileys.js";
import { sendMessageToAI } from "./api-client.js";

// Helper: Normalize JID by removing device suffix
function normalizeJid(jid: string): string {
  // Remove device ID suffix like :50 or :XX
  // Example: "5491126726818:50@s.whatsapp.net" -> "5491126726818@s.whatsapp.net"
  return jid.replace(/:\d+@/, "@");
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
  logger.debug(
    {
      botJid,
      mentionedJids,
      matches,
    },
    "Checking bot mention"
  );
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
    logger: logger.child({ module: "baileys" }),
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

  // Auto-response message handler
  sock.ev.on("messages.upsert", async ({ messages }) => {
    for (const msg of messages) {
      if (msg.key.fromMe || msg.key.remoteJid === "status@broadcast") continue;

      // Handle text messages
      const text = msg.message?.conversation || msg.message?.extendedTextMessage?.text;
      if (text) {
        const whatsappJid = normalizeJid(msg.key.remoteJid!);
        const conversationType = isGroupChat(whatsappJid) ? "group" : "private";
        const botJid = normalizeJid(sock.user!.id);

        // In groups, only respond if mentioned or replied to
        if (conversationType === "group" && !shouldRespondInGroup(msg, botJid)) {
          logger.debug({ whatsappJid }, "Skipping group message (not mentioned)");
          continue;
        }

        logger.info({ from: whatsappJid, text, conversationType }, "Received message");

        // Send typing indicator
        await sock.sendPresenceUpdate("composing", whatsappJid);

        try {
          // Call AI API with new queue endpoints
          const stream = await sendMessageToAI(whatsappJid, text, {
            conversationType,
            senderJid: msg.key.participant,
            senderName: getSenderName(msg),
          });

          // Accumulate response chunks
          let response = "";
          for await (const chunk of stream) {
            response += chunk;
          }

          // Send complete response
          await sock.sendMessage(whatsappJid, { text: response });

          logger.info(
            { to: whatsappJid, responseLength: response.length },
            "Sent AI response"
          );
        } catch (error) {
          logger.error({ error, whatsappJid }, "Error processing message");
          await sock.sendMessage(whatsappJid, {
            text: "Sorry, I encountered an error processing your message. Please try again.",
          });
        } finally {
          // Clear typing indicator
          await sock.sendPresenceUpdate("paused", whatsappJid);
        }

        continue;
      }

      // Handle audio messages
      const audioMessage = msg.message?.audioMessage;
      if (audioMessage) {
        const whatsappJid = normalizeJid(msg.key.remoteJid!);
        const conversationType = isGroupChat(whatsappJid) ? "group" : "private";
        const botJid = normalizeJid(sock.user!.id);

        // In groups, only respond if mentioned or replied to
        if (conversationType === "group" && !shouldRespondInGroup(msg, botJid)) {
          logger.debug({ whatsappJid }, "Skipping group audio (not mentioned)");
          continue;
        }

        logger.info({ from: whatsappJid, conversationType }, "Received audio message");
        await sock.sendPresenceUpdate("composing", whatsappJid);

        try {
          // Step 1: Download audio from WhatsApp
          const { downloadMediaMessage } = await import("@whiskeysockets/baileys");
          const buffer = await downloadMediaMessage(
            msg,
            "buffer",
            {},
            {
              logger: logger.child({ module: "baileys-download" }),
              reuploadRequest: sock.updateMediaMessage,
            }
          );

          if (!buffer) {
            throw new Error("Failed to download audio");
          }

          const mimetype = audioMessage.mimetype || "audio/ogg";
          const extension = mimetype.split("/")[1]?.split(";")[0] || "ogg";
          const filename = `audio_${Date.now()}.${extension}`;

          logger.info({ filename, size: buffer.length, mimetype }, "Audio downloaded");

          // Step 2: Transcribe audio (POST /transcribe)
          const blob = new Blob([buffer], { type: mimetype });
          const formData = new FormData();
          formData.append("file", blob, filename);
          // Optional: detect language and add to form
          // formData.append('language', 'en');

          const transcribeResponse = await fetch(`${process.env.AI_API_URL}/transcribe`, {
            method: "POST",
            body: formData,
            // No headers needed - fetch() handles multipart/form-data automatically
          });

          if (!transcribeResponse.ok) {
            const contentType = transcribeResponse.headers.get("content-type");
            if (contentType?.includes("application/json")) {
              const errorData = await transcribeResponse.json();
              const errorMsg = errorData.detail || "Transcription failed";

              // Special handling for 503 (service not configured)
              if (transcribeResponse.status === 503) {
                logger.error(
                  { status: 503, detail: errorMsg },
                  "STT service not configured"
                );
                throw new Error(`Transcription service not available: ${errorMsg}`);
              }

              throw new Error(errorMsg);
            } else {
              // Non-JSON response (unexpected)
              const text = await transcribeResponse.text();
              logger.error(
                { status: transcribeResponse.status, body: text },
                "Unexpected transcription error"
              );
              throw new Error(
                `Transcription failed with status ${transcribeResponse.status}`
              );
            }
          }

          const { transcription } = await transcribeResponse.json();
          logger.info({ transcription }, "Audio transcribed");

          // Step 3: Send transcribed text to AI (reuse existing chat flow)
          const stream = await sendMessageToAI(whatsappJid, transcription, {
            conversationType,
            senderJid: msg.key.participant,
            senderName: getSenderName(msg),
          });

          // Step 4: Accumulate and send AI response (same as text messages)
          let response = "";
          for await (const chunk of stream) {
            response += chunk;
          }

          await sock.sendMessage(whatsappJid, { text: response });

          logger.info(
            {
              to: whatsappJid,
              transcription,
              responseLength: response.length,
            },
            "Sent AI response to audio message"
          );
        } catch (error) {
          logger.error({ error, whatsappJid }, "Error processing audio");
          await sock.sendMessage(whatsappJid, {
            text: "Sorry, I could not process your audio message. Please try again or send text.",
          });
        } finally {
          await sock.sendPresenceUpdate("paused", whatsappJid);
        }

        continue;
      }
    }
  });
}
