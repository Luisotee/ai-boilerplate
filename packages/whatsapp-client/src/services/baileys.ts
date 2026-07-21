import type { WASocket } from '@whiskeysockets/baileys';

let baileysSocket: WASocket | null = null;
// True only while the connection is open (between the 'open' and 'close' events).
// Split out from `baileysSocket` so the socket can be tracked at *creation* — for
// teardown — while `isBaileysReady()` keeps meaning "the link is open" for senders.
let socketOpen = false;

/** Register the active socket. Called at socket *creation* so teardown can always
 *  reach it — including a socket created but not yet `open` (the initial-pairing window). */
export function setBaileysSocket(sock: WASocket): void {
  baileysSocket = sock;
}

/** Mark the connection open/closed. Only `true` between the 'open' and 'close' events. */
export function setSocketOpen(open: boolean): void {
  socketOpen = open;
}

export function clearBaileysSocket(): void {
  baileysSocket = null;
  socketOpen = false;
}

/** The current socket if one exists, even pre-`open`; never throws. For teardown paths. */
export function getLiveSocket(): WASocket | null {
  return baileysSocket;
}

export function getBaileysSocket(): WASocket {
  if (!baileysSocket) {
    throw new Error('Baileys socket not initialized. Please scan QR code first.');
  }
  return baileysSocket;
}

/** True only when a socket exists AND the connection is open — senders/health rely on this. */
export function isBaileysReady(): boolean {
  return baileysSocket !== null && socketOpen;
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
