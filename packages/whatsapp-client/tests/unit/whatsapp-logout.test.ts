/**
 * Unit tests for logoutWhatsApp() — the force re-pair orchestration.
 *
 * It must OWN the teardown → clear → re-init so the caller's success reflects the
 * real outcome: a failed cred wipe must surface (not silently report success), and
 * the re-init must be awaited (not fire an un-caught rejection that crashes the
 * process). The heavy collaborators (baileys, fs) are mocked at their seams.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// makeWASocket is referenced inside the baileys mock factory, so hoist it.
const { makeWASocket } = vi.hoisted(() => ({ makeWASocket: vi.fn() }));

vi.mock('../../src/services/baileys.js', () => ({
  isBaileysReady: vi.fn(),
  getBaileysSocket: vi.fn(),
  clearBaileysSocket: vi.fn(),
  setBaileysSocket: vi.fn(),
  setConnectionStatus: vi.fn(),
  setLatestQr: vi.fn(),
  getConnectionInfo: vi.fn(),
}));

// Keep the rest of fs/promises + baileys real; only swap the pieces logoutWhatsApp
// and initializeWhatsApp actually touch so no real socket opens or auth dir is rm'd.
vi.mock('node:fs/promises', async (importOriginal) => ({
  ...(await importOriginal<typeof import('node:fs/promises')>()),
  readdir: vi.fn().mockResolvedValue([]),
  rm: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@whiskeysockets/baileys', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@whiskeysockets/baileys')>()),
  default: makeWASocket,
  makeWASocket,
  useMultiFileAuthState: vi.fn().mockResolvedValue({ state: {}, saveCreds: vi.fn() }),
}));

import { logoutWhatsApp, initializeWhatsApp } from '../../src/whatsapp.js';
import {
  isBaileysReady,
  getBaileysSocket,
  clearBaileysSocket,
} from '../../src/services/baileys.js';
import { readdir, rm } from 'node:fs/promises';
import { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys';

const mockIsReady = isBaileysReady as ReturnType<typeof vi.fn>;
const mockGetSocket = getBaileysSocket as ReturnType<typeof vi.fn>;
const mockClearSocket = clearBaileysSocket as ReturnType<typeof vi.fn>;
const mockReaddir = readdir as unknown as ReturnType<typeof vi.fn>;
const mockRm = rm as unknown as ReturnType<typeof vi.fn>;

function makeSocket() {
  return {
    ev: { on: vi.fn(), removeAllListeners: vi.fn() },
    logout: vi.fn().mockResolvedValue(undefined),
    end: vi.fn(),
  };
}

describe('logoutWhatsApp', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockReaddir.mockResolvedValue(['creds.json']);
    mockRm.mockResolvedValue(undefined);
    // initializeWhatsApp() re-init issues a fresh socket; only .ev.on is exercised.
    makeWASocket.mockReturnValue({ ev: { on: vi.fn() } });
  });

  it('when connected: detaches the handler, logs out, drops the socket, clears creds and re-inits', async () => {
    const sock = makeSocket();
    mockIsReady.mockReturnValue(true);
    mockGetSocket.mockReturnValue(sock);

    await logoutWhatsApp();

    expect(sock.ev.removeAllListeners).toHaveBeenCalledWith('connection.update');
    expect(sock.logout).toHaveBeenCalledOnce();
    expect(mockClearSocket).toHaveBeenCalledOnce();
    expect(mockRm).toHaveBeenCalled(); // clearAuthState wiped the stored creds
    expect(makeWASocket).toHaveBeenCalledOnce(); // re-init issued a fresh socket
  });

  it('propagates a clearAuthState failure so the caller can report it (no silent success)', async () => {
    const sock = makeSocket();
    mockIsReady.mockReturnValue(true);
    mockGetSocket.mockReturnValue(sock);
    mockRm.mockRejectedValue(new Error('EROFS: read-only file system'));

    await expect(logoutWhatsApp()).rejects.toThrow('EROFS');
    expect(makeWASocket).not.toHaveBeenCalled(); // never re-inits after a failed clear
  });

  it('when logout() throws: ends the socket locally and still clears + re-inits', async () => {
    const sock = makeSocket();
    sock.logout.mockRejectedValue(new Error('socket already closed'));
    mockIsReady.mockReturnValue(true);
    mockGetSocket.mockReturnValue(sock);

    await logoutWhatsApp();

    expect(sock.end).toHaveBeenCalledOnce();
    expect(mockClearSocket).toHaveBeenCalledOnce();
    expect(makeWASocket).toHaveBeenCalledOnce();
  });

  it('when not ready: skips logout and just clears + re-inits', async () => {
    mockIsReady.mockReturnValue(false);

    await logoutWhatsApp();

    expect(mockGetSocket).not.toHaveBeenCalled();
    expect(mockRm).toHaveBeenCalled();
    expect(makeWASocket).toHaveBeenCalledOnce();
  });
});

/**
 * Tests for the connection.update 'close' handler — the passive reconnect + logged-out
 * self-heal paths. These are the half of the force-logout work that had no coverage: the
 * earlier mock swallowed listeners, so a synthetic close event could never be driven.
 *
 * The seam: makeWASocket returns a socket whose ev.on stores each handler in a Map, so a
 * test can fetch the registered 'connection.update' callback and drive close/loggedOut
 * updates. Fake timers + runOnlyPendingTimersAsync fire the backoff-scheduled reconnect
 * without caring about the exact (jittered) delay.
 */
describe('connection.update close handler', () => {
  const mockUseAuth = useMultiFileAuthState as unknown as ReturnType<typeof vi.fn>;

  type CapturingSocket = {
    handlers: Map<string, (update: unknown) => unknown>;
    ev: { on: ReturnType<typeof vi.fn>; removeAllListeners: ReturnType<typeof vi.fn> };
    logout: ReturnType<typeof vi.fn>;
    end: ReturnType<typeof vi.fn>;
  };

  let socks: CapturingSocket[];

  function makeCapturingSocket(): CapturingSocket {
    const handlers = new Map<string, (update: unknown) => unknown>();
    return {
      handlers,
      ev: {
        on: vi.fn((event: string, cb: (update: unknown) => unknown) => {
          handlers.set(event, cb);
        }),
        removeAllListeners: vi.fn(),
      },
      logout: vi.fn().mockResolvedValue(undefined),
      end: vi.fn(),
    };
  }

  // Drive the captured 'connection.update' listener of a given socket generation.
  function driveClose(sock: CapturingSocket, statusCode: number) {
    const handler = sock.handlers.get('connection.update');
    if (!handler) throw new Error('connection.update handler not registered');
    return handler({ connection: 'close', lastDisconnect: { error: { output: { statusCode } } } });
  }
  function driveOpen(sock: CapturingSocket) {
    const handler = sock.handlers.get('connection.update');
    if (!handler) throw new Error('connection.update handler not registered');
    // 'open' calls resetReconnectionState(), returning module state to clean between tests.
    return handler({ connection: 'open' });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    socks = [];
    mockUseAuth.mockResolvedValue({ state: {}, saveCreds: vi.fn() });
    mockReaddir.mockResolvedValue(['creds.json']);
    mockRm.mockResolvedValue(undefined);
    makeWASocket.mockReset();
    makeWASocket.mockImplementation(() => {
      const sock = makeCapturingSocket();
      socks.push(sock);
      return sock;
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('non-logged-out close: drops the socket and re-inits on the backoff timer', async () => {
    await initializeWhatsApp();
    expect(makeWASocket).toHaveBeenCalledTimes(1);

    await driveClose(socks[0], 515); // not loggedOut → should reconnect
    expect(mockClearSocket).toHaveBeenCalled(); // stopped reporting "ready" on close

    await vi.runOnlyPendingTimersAsync(); // fire the scheduled reconnect
    expect(makeWASocket).toHaveBeenCalledTimes(2); // re-inited

    await driveOpen(socks[1]);
  });

  it('a reconnect whose re-init throws reschedules itself and recovers (self-heal)', async () => {
    await initializeWhatsApp();

    // The next initializeWhatsApp() fails before a socket exists (no 'close' to re-arm),
    // so without the retry it would stall at disconnected forever.
    mockUseAuth.mockRejectedValueOnce(new Error('EMFILE: too many open files'));

    await driveClose(socks[0], 515);
    await vi.runOnlyPendingTimersAsync(); // fires reconnect → init throws → reschedules
    expect(makeWASocket).toHaveBeenCalledTimes(1); // failed before makeWASocket

    await vi.runOnlyPendingTimersAsync(); // fires the rescheduled reconnect → succeeds
    expect(makeWASocket).toHaveBeenCalledTimes(2); // self-healed

    await driveOpen(socks[1]);
  });

  it('logged-out close: wipes creds and re-inits to drop back into QR mode', async () => {
    await initializeWhatsApp();

    await driveClose(socks[0], DisconnectReason.loggedOut); // 401
    expect(mockRm).toHaveBeenCalled(); // clearAuthState wiped the stale creds
    expect(makeWASocket).toHaveBeenCalledTimes(2); // re-inited inline

    await driveOpen(socks[1]);
  });

  it('logged-out re-init failure self-heals via the backoff timer (no manual restart)', async () => {
    await initializeWhatsApp();

    mockRm.mockRejectedValueOnce(new Error('EROFS: read-only file system'));

    await driveClose(socks[0], DisconnectReason.loggedOut);
    expect(makeWASocket).toHaveBeenCalledTimes(1); // clearAuthState threw → never re-inited inline

    await vi.runOnlyPendingTimersAsync(); // the rescheduled reconnect re-inits
    expect(makeWASocket).toHaveBeenCalledTimes(2);

    await driveOpen(socks[1]);
  });
});
