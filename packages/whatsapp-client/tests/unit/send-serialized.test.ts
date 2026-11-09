/**
 * sendSerialized: every Baileys send runs on the socket that is live when the
 * send starts, is bounded by a timeout, and is ordered per chat (per-JID FIFO)
 * without making other chats wait.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../../src/config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/config.js')>();
  return { config: { ...actual.config, sendTimeoutMs: 1000 } };
});

type Mod = typeof import('../../src/services/baileys.js');
let mod: Mod;

function deferred<T = unknown>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function socket(sendMessage = vi.fn().mockResolvedValue({ key: { id: 'm1' } })) {
  return { sendMessage } as unknown as import('@whiskeysockets/baileys').WASocket & {
    sendMessage: ReturnType<typeof vi.fn>;
  };
}

function connect(sock: ReturnType<typeof socket>) {
  mod.setBaileysSocket(sock);
  mod.setSocketOpen(true);
}

describe('sendSerialized', () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    vi.resetModules();
    mod = await import('../../src/services/baileys.js');
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sends on the socket that is live when the send runs, not when it was queued', async () => {
    const oldSock = socket();
    connect(oldSock);
    // Simulate a reconnect between "message arrived" and "reply sent".
    mod.clearBaileysSocket();
    const newSock = socket();
    const sending = mod.sendSerialized('a@s.whatsapp.net', { text: 'hi' });
    connect(newSock);
    await vi.advanceTimersByTimeAsync(600);

    await expect(sending).resolves.toEqual({ key: { id: 'm1' } });
    expect(oldSock.sendMessage).not.toHaveBeenCalled();
    expect(newSock.sendMessage).toHaveBeenCalledWith('a@s.whatsapp.net', { text: 'hi' }, undefined);
  });

  it('runs sends to one chat in call order, one at a time', async () => {
    const first = deferred();
    const sendMessage = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValue({ key: { id: 'm2' } });
    connect(socket(sendMessage));

    const a = mod.sendSerialized('a@s.whatsapp.net', { text: '1' });
    const b = mod.sendSerialized('a@s.whatsapp.net', { text: '2' });
    await vi.advanceTimersByTimeAsync(0);
    expect(sendMessage).toHaveBeenCalledTimes(1);

    first.resolve({ key: { id: 'm1' } });
    await a;
    await b;
    expect(sendMessage.mock.calls.map((c) => c[1])).toEqual([{ text: '1' }, { text: '2' }]);
  });

  it('does not make other chats wait behind a slow send', async () => {
    const slow = deferred();
    const sendMessage = vi.fn((jid: string) =>
      jid === 'slow@s.whatsapp.net' ? slow.promise : Promise.resolve({ key: { id: 'fast' } })
    );
    connect(socket(sendMessage as never));

    void mod.sendSerialized('slow@s.whatsapp.net', { text: 'big upload' });
    await expect(mod.sendSerialized('b@s.whatsapp.net', { text: 'hi' })).resolves.toEqual({
      key: { id: 'fast' },
    });
    slow.resolve({ key: { id: 'slow' } });
  });

  it('times out a hung send and lets the next one in the chat run', async () => {
    const sendMessage = vi
      .fn()
      .mockImplementationOnce(() => new Promise(() => {})) // dead socket: never settles
      .mockResolvedValue({ key: { id: 'm2' } });
    connect(socket(sendMessage));

    const hung = mod.sendSerialized('a@s.whatsapp.net', { text: '1' });
    const hungResult = expect(hung).rejects.toBeInstanceOf(mod.SendTimeoutError);
    const next = mod.sendSerialized('a@s.whatsapp.net', { text: '2' });
    await vi.advanceTimersByTimeAsync(1000);

    await hungResult;
    await expect(next).resolves.toEqual({ key: { id: 'm2' } });
  });

  it('passes send errors through and keeps the queue going', async () => {
    const sendMessage = vi
      .fn()
      .mockRejectedValueOnce(new Error('not registered on WhatsApp'))
      .mockResolvedValue({ key: { id: 'm2' } });
    connect(socket(sendMessage));

    const failing = mod.sendSerialized('a@s.whatsapp.net', { text: '1' });
    const next = mod.sendSerialized('a@s.whatsapp.net', { text: '2' });
    await expect(failing).rejects.toThrow('not registered on WhatsApp');
    await expect(next).resolves.toEqual({ key: { id: 'm2' } });
  });

  it('fails with WhatsAppNotReadyError when the link stays down past the ready deadline', async () => {
    const sending = mod.sendSerialized('a@s.whatsapp.net', { text: 'hi' });
    const result = expect(sending).rejects.toBeInstanceOf(mod.WhatsAppNotReadyError);
    await vi.advanceTimersByTimeAsync(mod.SEND_READY_WAIT_MS);
    await result;
  });

  it('counts the ready deadline from enqueue, so a long outage does not stack waits', async () => {
    const a = mod.sendSerialized('a@s.whatsapp.net', { text: '1' });
    const b = mod.sendSerialized('a@s.whatsapp.net', { text: '2' });
    const ra = expect(a).rejects.toBeInstanceOf(mod.WhatsAppNotReadyError);
    const rb = expect(b).rejects.toBeInstanceOf(mod.WhatsAppNotReadyError);
    // Both were queued at t=0: both fail at ~15s, not 15s + 15s.
    await vi.advanceTimersByTimeAsync(mod.SEND_READY_WAIT_MS);
    await ra;
    await rb;
  });

  it('drops a chat queue once it is idle', async () => {
    connect(socket());
    await mod.sendSerialized('a@s.whatsapp.net', { text: '1' });
    await vi.advanceTimersByTimeAsync(0);
    expect(mod.pendingSendQueues()).toBe(0);
  });
});
