import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import qrcode from "qrcode-terminal";
import { logger } from "./logger.js";
import { setBaileysSocket } from "./services/baileys.js";
import { handleTextMessage } from "./handlers/text.js";
import { transcribeAudioMessage } from "./handlers/audio.js";

export async function initializeWhatsApp(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

  const sock = makeWASocket({
    auth: state,
    logger: logger.child({ module: "baileys" }),
  });

  // Connection events
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
      setBaileysSocket(sock);
    }
  });

  sock.ev.on("creds.update", saveCreds);

  // Message handler
  sock.ev.on('messages.upsert', async ({ messages }) => {
    for (const msg of messages) {
      if (msg.key.fromMe || msg.key.remoteJid === 'status@broadcast') continue;

      // Get text from message or transcribe audio
      let text = msg.message?.conversation || msg.message?.extendedTextMessage?.text;

      if (!text && msg.message?.audioMessage) {
        text = await transcribeAudioMessage(sock, msg);
      }

      if (text) {
        await handleTextMessage(sock, msg, text);
      }
    }
  });
}
