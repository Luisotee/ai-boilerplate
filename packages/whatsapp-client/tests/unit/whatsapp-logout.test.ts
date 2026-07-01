/**
 * Unit tests for logoutWhatsApp() — the force re-pair orchestration.
 *
 * It must OWN the teardown → clear → re-init so the caller's success reflects the
 * real outcome: a failed cred wipe must surface (not silently report success), and
 * the re-init must be awaited (not fire an un-caught rejection that crashes the
 * process). The heavy collaborators (baileys, fs) are mocked at their seams.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

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

import { logoutWhatsApp } from '../../src/whatsapp.js';
import { isBaileysReady, getBaileysSocket, clearBaileysSocket } from '../../src/services/baileys.js';
import { readdir, rm } from 'node:fs/promises';

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
