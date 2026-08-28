/**
 * Placement regression test for the whitelist gate in whatsapp.ts.
 *
 * The bug this guards is a ORDERING bug, and ordering is invisible to every
 * other test: before this file existed you could move the gate back above
 * `resolveSenderPhone` and all 896 tests still passed, silently restoring the
 * original defect (a bare phone never matching a LID-addressed chat).
 *
 * The gate must sit in a narrow window:
 *   - AFTER  resolveSenderPhone — no phone exists before it, so a LID chat
 *            would fail closed and a whitelisted contact would be dropped.
 *   - BEFORE `sock.user!.id` — if sock.user is unset that deref throws into the
 *            per-message catch, which calls sendFailureReaction, i.e. it would
 *            react into a chat that was just blocked.
 * Both bounds are asserted below.
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

// Unstubbed, initializeWhatsApp() would resolve the WA Web version over the
// network and add a 5s timeout per call.
vi.mock('../../src/services/wa-version.js', () => ({
  getWaVersionConfig: vi.fn().mockResolvedValue({}),
}));

// The assertion target and the secondary canary. utils/jid.js is deliberately
// NOT mocked — resolveSenderPhone is the thing whose ordering is under test.
vi.mock('../../src/handlers/text.js', () => ({ handleTextMessage: vi.fn() }));
vi.mock('../../src/utils/reactions.js', () => ({ sendFailureReaction: vi.fn() }));

const PHONE = '4915755945319';
const OK_LID = '109994229891095@lid';
const BAD_LID = '999999999999999@lid';

/** A LID-addressed text message whose phone is recoverable from remoteJidAlt. */
function makeLidTextMsg(lid: string, altPhone: string, text = 'hi') {
  return {
    key: { remoteJid: lid, remoteJidAlt: `${altPhone}@s.whatsapp.net`, fromMe: false, id: 'M1' },
    message: { conversation: text },
    pushName: 'Someone',
  };
}

describe('whitelist gate placement in messages.upsert', () => {
  let userSpy: ReturnType<typeof vi.fn>;
  let handlers: Map<string, (arg: unknown) => unknown>;
  let handleTextMessage: ReturnType<typeof vi.fn>;
  let sendFailureReaction: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    vi.resetModules();
    vi.clearAllMocks();
    // config.ts computes whitelistPhones at module load, and loads the root
    // .env WITHOUT override — so a stubbed process.env value wins.
    vi.stubEnv('WHITELIST_PHONES', PHONE);

    handlers = new Map();
    userSpy = vi.fn(() => ({ id: '5511000000000:42@s.whatsapp.net', lid: '123456789:42@lid' }));
    const sock: Record<string, unknown> = {
      ev: {
        on: vi.fn((event: string, cb: (arg: unknown) => unknown) => handlers.set(event, cb)),
      },
      sendPresenceUpdate: vi.fn(),
      groupMetadata: vi.fn(),
    };
    // A getter spy asserts the "gate precedes the sock.user deref" bound
    // directly, instead of inferring it from a thrown error.
    Object.defineProperty(sock, 'user', { get: userSpy, configurable: true });
    makeWASocket.mockReturnValue(sock);

    const { initializeWhatsApp } = await import('../../src/whatsapp.js');
    await initializeWhatsApp();

    // Guard against a silent pass on the wrong whitelist.
    const { config } = await import('../../src/config.js');
    expect(config.whitelistPhones.phones.has(PHONE)).toBe(true);

    ({ handleTextMessage } = (await import('../../src/handlers/text.js')) as never);
    ({ sendFailureReaction } = (await import('../../src/utils/reactions.js')) as never);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function driveUpsert(msg: unknown) {
    const handler = handlers.get('messages.upsert');
    if (!handler) throw new Error('messages.upsert handler was never registered');
    await handler({ messages: [msg], type: 'notify' });
  }

  // Fails if the gate moves back above resolveSenderPhone: `phone` would be
  // undefined at gate time, isWhitelisted(wl, '…@lid', undefined) fails closed,
  // and this whitelisted message would be dropped.
  it('processes a LID-addressed message whose phone resolves via remoteJidAlt', async () => {
    await driveUpsert(makeLidTextMsg(OK_LID, PHONE));

    expect(handleTextMessage).toHaveBeenCalledOnce();
    // The options arg carries the resolved identity — a second, independent
    // witness that resolution ran before the gate let the message through.
    expect(handleTextMessage).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      'hi',
      undefined,
      undefined,
      expect.objectContaining({ phone: `+${PHONE}`, whatsappLid: OK_LID })
    );
    // Sanity: proves the getter spy is actually wired, so the negative test's
    // not-called assertion cannot pass vacuously.
    expect(userSpy).toHaveBeenCalled();
  });

  // The unlisted LID resolves too, so this can only fail for the whitelist
  // verdict — never because identity resolution came back empty.
  it('drops a non-whitelisted LID without touching sock.user or reacting', async () => {
    await driveUpsert(makeLidTextMsg(BAD_LID, '4915700000000'));

    expect(handleTextMessage).not.toHaveBeenCalled();
    expect(userSpy).not.toHaveBeenCalled(); // gate precedes the sock.user deref
    expect(sendFailureReaction).not.toHaveBeenCalled(); // nothing reacted into a blocked chat
  });
});
