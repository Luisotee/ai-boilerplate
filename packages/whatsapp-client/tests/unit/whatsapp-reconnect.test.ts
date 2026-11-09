/**
 * Reconnect policy: the attempt counter resets only after the link stays up
 * for RECONNECT_STABLE_MS (so open→close flaps keep backing off), past
 * RECONNECT_MAX_ATTEMPTS the retry interval is the long one, and a 440 session
 * conflict waits RECONNECT_CONFLICT_DELAY_MS and is reported to Sentry.
 *
 * Each test re-imports whatsapp.ts so its module-level reconnect state starts
 * clean. Math.random is pinned to 0, so delays carry no jitter.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const { makeWASocket, captureMessage } = vi.hoisted(() => ({
  makeWASocket: vi.fn(),
  captureMessage: vi.fn(),
}));

vi.mock('../../src/config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/config.js')>();
  return {
    config: {
      ...actual.config,
      reconnection: {
        initialDelayMs: 1000,
        maxDelayMs: 30000,
        maxAttempts: 3,
        jitterMs: 500,
        longDelayMs: 300000,
        stableMs: 60000,
        conflictDelayMs: 900000,
      },
    },
  };
});

vi.mock('../../src/instrument.js', () => ({ Sentry: { captureMessage } }));

vi.mock('../../src/services/baileys.js', () => ({
  isBaileysReady: vi.fn(),
  getLiveSocket: vi.fn(),
  clearBaileysSocket: vi.fn(),
  setBaileysSocket: vi.fn(),
  setSocketOpen: vi.fn(),
  setConnectionStatus: vi.fn(),
  setLatestQr: vi.fn(),
  setPairedPhoneNumber: vi.fn(),
  getPairedPhoneNumber: vi.fn(),
  getConnectionInfo: vi.fn(),
  sendSerialized: vi.fn(),
}));

vi.mock('@whiskeysockets/baileys', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@whiskeysockets/baileys')>()),
  default: makeWASocket,
  makeWASocket,
  useMultiFileAuthState: vi.fn().mockResolvedValue({ state: {}, saveCreds: vi.fn() }),
}));

// Route the atomic auth-state wrapper to the mocked useMultiFileAuthState above,
// so each init's saveCreds spy stays observable and no real files are touched.
vi.mock('../../src/services/auth-state.js', async () => {
  const baileys = await import('@whiskeysockets/baileys');
  return {
    useAtomicMultiFileAuthState: (dir: string) => baileys.useMultiFileAuthState(dir),
    flushPendingCredsSave: vi.fn().mockResolvedValue(true),
  };
});

vi.mock('../../src/services/wa-version.js', () => ({
  getWaVersionConfig: vi.fn().mockResolvedValue({}),
}));

import { DisconnectReason } from '@whiskeysockets/baileys';

type Handler = (update: unknown) => unknown;
type CapturingSocket = { handlers: Map<string, Handler> };

let socks: CapturingSocket[];
let initializeWhatsApp: () => Promise<void>;
let shutdownWhatsApp: () => Promise<void>;

function makeCapturingSocket(): CapturingSocket & Record<string, unknown> {
  const handlers = new Map<string, Handler>();
  return {
    handlers,
    ev: {
      on: vi.fn((event: string, cb: Handler) => handlers.set(event, cb)),
      removeAllListeners: vi.fn(),
    },
    end: vi.fn().mockResolvedValue(undefined),
  };
}

const latest = () => socks[socks.length - 1];
const update = (u: unknown) => latest().handlers.get('connection.update')!(u);
const open = () => update({ connection: 'open' });
const close = (statusCode: number) =>
  update({ connection: 'close', lastDisconnect: { error: { output: { statusCode } } } });

/** Advance until the next scheduled reconnect re-inits; return how long that took. */
async function msUntilReconnect(): Promise<number> {
  const before = socks.length;
  let waited = 0;
  while (socks.length === before) {
    await vi.advanceTimersByTimeAsync(1000);
    waited += 1000;
    if (waited > 2_000_000) throw new Error('no reconnect scheduled');
  }
  return waited;
}

describe('reconnect policy', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(0);
    socks = [];
    makeWASocket.mockImplementation(() => {
      const sock = makeCapturingSocket();
      socks.push(sock);
      return sock;
    });
    vi.resetModules();
    ({ initializeWhatsApp, shutdownWhatsApp } = await import('../../src/whatsapp.js'));
    await initializeWhatsApp();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('keeps backing off when the link opens and drops again before it is stable', async () => {
    await close(DisconnectReason.connectionClosed);
    expect(await msUntilReconnect()).toBe(1000); // attempt 1: 1s

    await open();
    await vi.advanceTimersByTimeAsync(5000); // drops well before stableMs
    await close(DisconnectReason.connectionClosed);
    expect(await msUntilReconnect()).toBe(2000); // attempt 2: 2s, not back to 1s
  });

  it('resets the backoff once the link has stayed open for stableMs', async () => {
    await close(DisconnectReason.connectionClosed);
    await msUntilReconnect();
    await open();
    await vi.advanceTimersByTimeAsync(60000); // stable

    await close(DisconnectReason.connectionClosed);
    expect(await msUntilReconnect()).toBe(1000); // back to attempt 1
  });

  it('switches to the long interval after maxAttempts failures in a row', async () => {
    for (const expected of [1000, 2000, 4000]) {
      await close(DisconnectReason.connectionClosed);
      expect(await msUntilReconnect()).toBe(expected);
    }
    await close(DisconnectReason.connectionClosed);
    expect(await msUntilReconnect()).toBe(300000);
  });

  it('waits the conflict delay on 440 and reports it to Sentry', async () => {
    await close(DisconnectReason.connectionReplaced);
    expect(captureMessage).toHaveBeenCalledWith('WhatsApp session conflict (440)', 'warning');
    expect(await msUntilReconnect()).toBe(900000);
  });

  it('after shutdownWhatsApp, ends the socket, flushes creds and never reconnects', async () => {
    const { getLiveSocket } = await import('../../src/services/baileys.js');
    const { flushPendingCredsSave } = await import('../../src/services/auth-state.js');
    const sock = latest() as unknown as { end: ReturnType<typeof vi.fn> };
    (getLiveSocket as ReturnType<typeof vi.fn>).mockReturnValue(sock);

    await shutdownWhatsApp();
    await vi.advanceTimersByTimeAsync(0);
    expect(sock.end).toHaveBeenCalled();
    expect(flushPendingCredsSave).toHaveBeenCalled();

    await close(DisconnectReason.connectionClosed);
    await vi.advanceTimersByTimeAsync(2_000_000);
    expect(socks).toHaveLength(1);
  });
});
