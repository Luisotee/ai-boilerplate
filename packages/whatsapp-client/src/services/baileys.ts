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
