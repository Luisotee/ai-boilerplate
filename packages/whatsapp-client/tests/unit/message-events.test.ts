import { describe, it, expect, vi, beforeEach } from 'vitest';
import { WAMessageStubType } from '@whiskeysockets/baileys';

vi.mock('../../src/logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));
vi.mock('../../src/api-client.js', () => ({
  editMessage: vi.fn(async () => 1),
  revokeMessage: vi.fn(async () => 1),
}));
vi.mock('../../src/services/group-admin.js', () => ({
  resolveSenderGroupAdmin: vi.fn(async () => true),
}));

import {
  classifyMessageUpdate,
  eventInScope,
  handleMessageUpdate,
} from '../../src/handlers/message-events.js';
import { editMessage, revokeMessage } from '../../src/api-client.js';
import { resolveSenderGroupAdmin } from '../../src/services/group-admin.js';
import { parseWhitelist } from '../../src/utils/whitelist.js';

const GROUP = '120363000000000001@g.us';
const ALICE = '5511111111111@s.whatsapp.net';
const PRIVATE = '5511999999999@s.whatsapp.net';

const sock = {
  signalRepository: { lidMapping: { getPNForLID: vi.fn(async () => null) } },
} as never;

function revoke(remoteJid: string, participant?: string, extra = {}) {
  return {
    key: { remoteJid, participant, id: 'M1', fromMe: false, ...extra },
    update: { message: null, messageStubType: WAMessageStubType.REVOKE },
  } as never;
}

function edit(remoteJid: string, participant: string | undefined, inner: object) {
  return {
    key: { remoteJid, participant, id: 'M1', fromMe: false },
    update: { message: { editedMessage: { message: inner } } },
  } as never;
}

const open = async () => parseWhitelist('');

beforeEach(() => vi.clearAllMocks());

describe('classifyMessageUpdate', () => {
  it('recognises a revoke', () => {
    expect(classifyMessageUpdate(revoke(GROUP, ALICE))).toEqual({ kind: 'revoke' });
  });

  it('recognises a text edit, plain or extended', () => {
    expect(classifyMessageUpdate(edit(GROUP, ALICE, { conversation: 'new' }))).toEqual({
      kind: 'edit',
      text: 'new',
    });
    expect(
      classifyMessageUpdate(edit(GROUP, ALICE, { extendedTextMessage: { text: 'new2' } }))
    ).toEqual({ kind: 'edit', text: 'new2' });
  });

  it('flags a caption edit as not handled', () => {
    expect(classifyMessageUpdate(edit(GROUP, ALICE, { imageMessage: { caption: 'c' } }))).toEqual({
      kind: 'edit-no-text',
    });
  });

  it('ignores our own messages, status, receipts and other updates', () => {
    expect(classifyMessageUpdate(revoke(GROUP, ALICE, { fromMe: true }))).toBeNull();
    expect(classifyMessageUpdate(revoke('status@broadcast', ALICE))).toBeNull();
    expect(
      classifyMessageUpdate({ key: { remoteJid: GROUP, id: 'M1' }, update: { status: 3 } } as never)
    ).toBeNull();
  });
});

describe('eventInScope', () => {
  it('fails closed when the whitelist is unavailable', () => {
    expect(eventInScope(null, PRIVATE, false, '+5511999999999')).toBe(false);
  });

  it('passes everything when the whitelist is off', () => {
    expect(eventInScope(parseWhitelist(''), PRIVATE, false, undefined)).toBe(true);
  });

  it('checks the chat against the whitelist', () => {
    const wl = parseWhitelist('5511999999999');
    expect(eventInScope(wl, PRIVATE, false, '+5511999999999')).toBe(true);
    expect(eventInScope(wl, GROUP, true, undefined)).toBe(false);
  });
});

describe('handleMessageUpdate', () => {
  it('forwards a group revoke with the deleter and their admin status', async () => {
    await handleMessageUpdate(sock, revoke(GROUP, ALICE), open);
    expect(resolveSenderGroupAdmin).toHaveBeenCalledOnce();
    expect(revokeMessage).toHaveBeenCalledWith(
      { whatsappJid: GROUP, messageId: 'M1', senderJid: ALICE, phone: undefined },
      true
    );
  });

  it('forwards a private revoke without an admin lookup', async () => {
    await handleMessageUpdate(sock, revoke(PRIVATE), open);
    expect(resolveSenderGroupAdmin).not.toHaveBeenCalled();
    expect(revokeMessage).toHaveBeenCalledWith(
      expect.objectContaining({ whatsappJid: PRIVATE, messageId: 'M1', phone: '+5511999999999' }),
      undefined
    );
  });

  it('forwards a text edit with the editor', async () => {
    await handleMessageUpdate(sock, edit(GROUP, ALICE, { conversation: 'fixed typo' }), open);
    expect(editMessage).toHaveBeenCalledWith(
      expect.objectContaining({ whatsappJid: GROUP, messageId: 'M1', senderJid: ALICE }),
      'fixed typo'
    );
  });

  it('sends nothing for an out-of-scope chat or an unavailable whitelist', async () => {
    await handleMessageUpdate(sock, revoke(PRIVATE), async () => parseWhitelist('5500000000000'));
    await handleMessageUpdate(sock, revoke(PRIVATE), async () => null);
    expect(revokeMessage).not.toHaveBeenCalled();
  });

  it('never throws', async () => {
    vi.mocked(revokeMessage).mockRejectedValueOnce(new Error('boom'));
    await expect(handleMessageUpdate(sock, revoke(GROUP, ALICE), open)).resolves.toBeUndefined();
  });
});
