import type { WASocket } from '@whiskeysockets/baileys';

let baileysSocket: WASocket | null = null;

export function setBaileysSocket(sock: WASocket): void {
  baileysSocket = sock;
}

export function getBaileysSocket(): WASocket {
  if (!baileysSocket) {
    throw new Error('Baileys socket not initialized. Please scan QR code first.');
  }
  return baileysSocket;
}

export function isBaileysReady(): boolean {
  return baileysSocket !== null;
}

// ── Connection / pairing state ───────────────────────────────────────────────
// Tracks the WhatsApp link lifecycle so the pairing QR can be surfaced over HTTP
// (e.g. for the management dashboard) instead of only being printed to the
// terminal. The QR is sensitive — it links a device to this bot's WhatsApp
// account — so the route that exposes it stays behind the API key.

export type WhatsAppConnectionStatus = 'connecting' | 'qr' | 'connected' | 'disconnected';

export interface WhatsAppConnectionInfo {
  status: WhatsAppConnectionStatus;
  qr: string | null;
  qrGeneratedAt: string | null;
}

let connectionStatus: WhatsAppConnectionStatus = 'connecting';
let latestQr: string | null = null;
let qrGeneratedAt: string | null = null;

export function setConnectionStatus(status: WhatsAppConnectionStatus): void {
  connectionStatus = status;
}

/** Store the latest pairing QR (or clear it once paired). */
export function setLatestQr(qr: string | null): void {
  latestQr = qr;
  qrGeneratedAt = qr ? new Date().toISOString() : null;
}

export function getConnectionInfo(): WhatsAppConnectionInfo {
  return { status: connectionStatus, qr: latestQr, qrGeneratedAt };
}
