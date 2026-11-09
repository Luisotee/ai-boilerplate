import type { WASocket } from '@whiskeysockets/baileys';
import { config } from '../config.js';

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

// ── Send path ────────────────────────────────────────────────────────────────
// Every outbound Baileys message goes through `sendSerialized`, which:
// - takes the socket that is live *when the send runs*, never one captured
//   earlier (a reply can be sent up to 120s after its message arrived, and a
//   reconnect in between leaves the captured socket dead);
// - bounds each send with `config.sendTimeoutMs`, because a send on a silently
//   dead socket can hang forever;
// - runs sends to the same chat one at a time, in call order (a per-JID FIFO).
//   Chats don't wait for each other, so one slow media upload never holds every
//   other user's reply. Exception: a send that times out keeps running in
//   Baileys (it can't be cancelled), so the next send to that chat may overlap
//   it and arrive first. Holding the queue until it settles would bring back
//   the unbounded hang the timeout exists to prevent.

type SendArgs = Parameters<WASocket['sendMessage']>;
type SendResult = Awaited<ReturnType<WASocket['sendMessage']>>;

/** How long a send waits for the link to (re)open, counted from when it was queued. */
export const SEND_READY_WAIT_MS = 15_000;
const READY_POLL_MS = 500;

export class SendTimeoutError extends Error {
  constructor(timeoutMs: number) {
    super(`WhatsApp send timed out after ${timeoutMs}ms`);
    this.name = 'SendTimeoutError';
  }
}

export class WhatsAppNotReadyError extends Error {
  constructor() {
    super('WhatsApp not connected');
    this.name = 'WhatsAppNotReadyError';
  }
}

// Set at process shutdown: queued and new sends fail at once instead of
// waiting for a link that won't reopen, so SIGTERM finishes inside Docker's grace.
let sendsStopped = false;

/** Fail every queued and future send immediately (process shutdown). */
export function stopSends(): void {
  sendsStopped = true;
}

// Tail of each chat's queue. The stored promise never rejects; an entry is
// dropped once its chain goes idle, so the map only holds chats with sends.
const sendQueues = new Map<string, Promise<void>>();

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function withSendTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  if (timeoutMs <= 0) return promise;
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new SendTimeoutError(timeoutMs)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function runSend(
  jid: string,
  content: SendArgs[1],
  options: SendArgs[2],
  readyDeadline: number
): Promise<SendResult> {
  if (sendsStopped) throw new WhatsAppNotReadyError();
  while (!isBaileysReady()) {
    const left = readyDeadline - Date.now();
    if (left <= 0 || sendsStopped) throw new WhatsAppNotReadyError();
    await sleep(Math.min(READY_POLL_MS, left));
  }
  const sock = baileysSocket!;
  return withSendTimeout(sock.sendMessage(jid, content, options), config.sendTimeoutMs);
}

/** Send one message on the live socket, after earlier sends to the same chat. */
export function sendSerialized(
  jid: string,
  content: SendArgs[1],
  options?: SendArgs[2]
): Promise<SendResult> {
  const readyDeadline = Date.now() + SEND_READY_WAIT_MS;
  const previous = sendQueues.get(jid) ?? Promise.resolve();
  const job = previous.then(() => runSend(jid, content, options, readyDeadline));
  const tail = job.then(
    () => undefined,
    () => undefined
  );
  sendQueues.set(jid, tail);
  void tail.then(() => {
    if (sendQueues.get(jid) === tail) sendQueues.delete(jid);
  });
  return job;
}

/** Number of chats with a queued or running send (for tests). */
export function pendingSendQueues(): number {
  return sendQueues.size;
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
  phoneNumber: string | null;
}

let connectionStatus: WhatsAppConnectionStatus = 'connecting';
let latestQr: string | null = null;
let qrGeneratedAt: string | null = null;
let pairedPhoneNumber: string | null = null;

export function setConnectionStatus(status: WhatsAppConnectionStatus): void {
  connectionStatus = status;
}

/** Store the paired bot phone number digits (survives socket reconnects). */
export function setPairedPhoneNumber(phone: string | null): void {
  pairedPhoneNumber = phone;
}

export function getPairedPhoneNumber(): string | null {
  return pairedPhoneNumber;
}

/** Store the latest pairing QR (or clear it once paired). */
export function setLatestQr(qr: string | null): void {
  latestQr = qr;
  qrGeneratedAt = qr ? new Date().toISOString() : null;
}

export function getConnectionInfo(): WhatsAppConnectionInfo {
  return { status: connectionStatus, qr: latestQr, qrGeneratedAt, phoneNumber: pairedPhoneNumber };
}

// ── Connection health (for GET /health) ──────────────────────────────────────
// Lets an operator spot a zombie link ("connected" but nothing arriving) and a
// reconnect loop without reading logs.

export interface LastDisconnect {
  at: string;
  statusCode: number | null;
  reason: string | null;
}

let lastInboundAt: string | null = null;
let lastDisconnect: LastDisconnect | null = null;
let reconnectAttempts = 0;

/** Record that a message arrived from WhatsApp. */
export function noteInbound(): void {
  lastInboundAt = new Date().toISOString();
}

export function setLastDisconnect(statusCode: number | null, reason: string | null): void {
  lastDisconnect = {
    at: new Date().toISOString(),
    statusCode,
    reason: reason?.slice(0, 200) ?? null,
  };
}

export function setReconnectAttempts(attempts: number): void {
  reconnectAttempts = attempts;
}

export function getConnectionHealth(): {
  lastInboundAt: string | null;
  lastDisconnect: LastDisconnect | null;
  reconnectAttempts: number;
} {
  return { lastInboundAt, lastDisconnect, reconnectAttempts };
}
