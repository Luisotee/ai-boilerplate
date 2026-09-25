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
  getLiveSocket: vi.fn(),
  clearBaileysSocket: vi.fn(),
  setBaileysSocket: vi.fn(),
  setSocketOpen: vi.fn(),
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

// initializeWhatsApp() resolves the WA Web version on every init; stub it so tests never
// hit the network (the real fetch would add a 5s timeout per uncached call).
vi.mock('../../src/services/wa-version.js', () => ({
  getWaVersionConfig: vi.fn().mockResolvedValue({}),
}));

import { logoutWhatsApp, initializeWhatsApp } from '../../src/whatsapp.js';
import {
  isBaileysReady,
  getLiveSocket,
  clearBaileysSocket,
  setSocketOpen,
} from '../../src/services/baileys.js';
import { readdir, rm } from 'node:fs/promises';
import { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys';

const mockIsReady = isBaileysReady as ReturnType<typeof vi.fn>;
const mockGetLiveSocket = getLiveSocket as ReturnType<typeof vi.fn>;
const mockClearSocket = clearBaileysSocket as ReturnType<typeof vi.fn>;
const mockSetSocketOpen = setSocketOpen as ReturnType<typeof vi.fn>;
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

  it('when connected: detaches both handlers, logs out, drops the socket, clears creds and re-inits', async () => {
    const sock = makeSocket();
    mockGetLiveSocket.mockReturnValue(sock);
    mockIsReady.mockReturnValue(true); // connection is open

    await logoutWhatsApp();

    expect(sock.ev.removeAllListeners).toHaveBeenCalledWith('connection.update');
    expect(sock.ev.removeAllListeners).toHaveBeenCalledWith('creds.update');
    expect(sock.logout).toHaveBeenCalledOnce();
    expect(sock.end).not.toHaveBeenCalled(); // logout() succeeded — no local end needed
    expect(mockClearSocket).toHaveBeenCalledOnce();
    expect(mockRm).toHaveBeenCalled(); // clearAuthState wiped the stored creds
    expect(makeWASocket).toHaveBeenCalledOnce(); // re-init issued a fresh socket
  });

  it('when a socket exists but is pre-open: ends it locally WITHOUT logout(), then clears + re-inits', async () => {
    const sock = makeSocket();
    mockGetLiveSocket.mockReturnValue(sock);
    mockIsReady.mockReturnValue(false); // socket created but not yet 'open' (initial pairing)

    await logoutWhatsApp();

    expect(sock.ev.removeAllListeners).toHaveBeenCalledWith('connection.update');
    expect(sock.ev.removeAllListeners).toHaveBeenCalledWith('creds.update');
    expect(sock.logout).not.toHaveBeenCalled(); // never logout() a pre-open socket (can hang the WS)
    expect(sock.end).toHaveBeenCalledOnce(); // ended locally instead
    expect(mockClearSocket).toHaveBeenCalledOnce();
    expect(mockRm).toHaveBeenCalled();
    expect(makeWASocket).toHaveBeenCalledOnce();
  });

  it('propagates a clearAuthState failure so the caller can report it (no silent success)', async () => {
    const sock = makeSocket();
    mockGetLiveSocket.mockReturnValue(sock);
    mockIsReady.mockReturnValue(true);
    mockRm.mockRejectedValue(new Error('EROFS: read-only file system'));

    await expect(logoutWhatsApp()).rejects.toThrow('EROFS');
    expect(makeWASocket).not.toHaveBeenCalled(); // never re-inits after a failed clear
  });

  it('when logout() throws: ends the socket locally and still clears + re-inits', async () => {
    const sock = makeSocket();
    sock.logout.mockRejectedValue(new Error('socket already closed'));
    mockGetLiveSocket.mockReturnValue(sock);
    mockIsReady.mockReturnValue(true);

    await logoutWhatsApp();

    expect(sock.end).toHaveBeenCalledOnce();
    expect(mockClearSocket).toHaveBeenCalledOnce();
    expect(makeWASocket).toHaveBeenCalledOnce();
  });

  it('when no live socket exists: skips teardown and just clears + re-inits', async () => {
    mockGetLiveSocket.mockReturnValue(null);

    await logoutWhatsApp();

    expect(mockClearSocket).not.toHaveBeenCalled(); // nothing to tear down
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

/**
 * Tests for the socket-generation (epoch) guard — the fix for the pre-open unlink race
 * (issue #4). Each init claims a generation; the connection.update + creds.update handlers
 * capture their generation and go inert once superseded. The load-bearing case: a stale
 * socket's late creds.update must NOT re-persist the auth state a concurrent logout wiped.
 *
 * The seam: each init gets its own saveCreds spy (via useMultiFileAuthState), and the
 * capturing socket stores its handlers so a test can drive a *specific* generation's events
 * after a newer one has been claimed.
 */
describe('socket generation (epoch) guard', () => {
  const mockUseAuth = useMultiFileAuthState as unknown as ReturnType<typeof vi.fn>;

  type CapturingSocket = {
    handlers: Map<string, (update: unknown) => unknown>;
    ev: { on: ReturnType<typeof vi.fn>; removeAllListeners: ReturnType<typeof vi.fn> };
    logout: ReturnType<typeof vi.fn>;
    end: ReturnType<typeof vi.fn>;
  };

  let socks: CapturingSocket[];
  let saveCredsSpies: Array<ReturnType<typeof vi.fn>>;

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

  function driveCreds(sock: CapturingSocket) {
    const handler = sock.handlers.get('creds.update');
    if (!handler) throw new Error('creds.update handler not registered');
    return handler(undefined);
  }
  function driveClose(sock: CapturingSocket, statusCode: number) {
    const handler = sock.handlers.get('connection.update');
    if (!handler) throw new Error('connection.update handler not registered');
    return handler({ connection: 'close', lastDisconnect: { error: { output: { statusCode } } } });
  }
  function driveOpen(sock: CapturingSocket) {
    const handler = sock.handlers.get('connection.update');
    if (!handler) throw new Error('connection.update handler not registered');
    return handler({ connection: 'open' });
  }

  beforeEach(() => {
    vi.clearAllMocks();
    socks = [];
    saveCredsSpies = [];
    // Each init gets its own saveCreds spy so calls can be attributed to a generation.
    mockUseAuth.mockImplementation(async () => {
      const saveCreds = vi.fn();
      saveCredsSpies.push(saveCreds);
      return { state: {}, saveCreds };
    });
    mockReaddir.mockResolvedValue(['creds.json']);
    mockRm.mockResolvedValue(undefined);
    makeWASocket.mockReset();
    makeWASocket.mockImplementation(() => {
      const sock = makeCapturingSocket();
      socks.push(sock);
      return sock;
    });
  });

  it('a superseded generation cannot re-persist creds after re-init (resurrection guard)', async () => {
    await initializeWhatsApp(); // gen1
    expect(makeWASocket).toHaveBeenCalledTimes(1);

    // gen1's own creds.update still persists while it is the current generation.
    await driveCreds(socks[0]);
    expect(saveCredsSpies[0]).toHaveBeenCalledOnce();

    // Simulate the pre-open unlink: getLiveSocket returns gen1, but it isn't open.
    mockGetLiveSocket.mockReturnValue(socks[0]);
    mockIsReady.mockReturnValue(false);

    await logoutWhatsApp(); // bumps epoch, wipes creds, re-inits gen2
    expect(makeWASocket).toHaveBeenCalledTimes(2);

    // The resurrection vector: gen1's socket emits a late creds.update AFTER the wipe.
    // The epoch guard must drop it so the wiped auth state stays wiped.
    saveCredsSpies[0].mockClear();
    await driveCreds(socks[0]);
    expect(saveCredsSpies[0]).not.toHaveBeenCalled();

    // Sanity: the fresh generation's own creds.update still persists.
    await driveCreds(socks[1]);
    expect(saveCredsSpies[1]).toHaveBeenCalledOnce();
  });

  it("a superseded generation's open/close events are inert (no clobbering the new socket)", async () => {
    await initializeWhatsApp(); // gen1
    mockGetLiveSocket.mockReturnValue(socks[0]);
    mockIsReady.mockReturnValue(false);
    await logoutWhatsApp(); // → gen2
    expect(makeWASocket).toHaveBeenCalledTimes(2);

    // Clear the recorded service-mock calls so we can attribute the next ones cleanly.
    mockClearSocket.mockClear();
    mockSetSocketOpen.mockClear();

    // Drive gen1's stale connection.update: 'close' would normally clearBaileysSocket()
    // and 'open' would setSocketOpen(true) — the epoch guard must make both no-ops.
    await driveClose(socks[0], 515);
    await driveOpen(socks[0]);

    expect(mockClearSocket).not.toHaveBeenCalled();
    expect(mockSetSocketOpen).not.toHaveBeenCalled();
  });
});
