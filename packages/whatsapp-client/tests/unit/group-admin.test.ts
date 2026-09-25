import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), trace: vi.fn() },
}));

import { looksLikeCommand, resolveSenderGroupAdmin } from '../../src/services/group-admin.js';
import { clearGroupCache } from '../../src/services/group-cache.js';

const GROUP = '120363012345678@g.us';
const ADMIN = '5511999999999@s.whatsapp.net';
const MEMBER = '5511888888888@s.whatsapp.net';

function makeSock() {
  return {
    groupMetadata: vi.fn().mockResolvedValue({
      id: GROUP,
      subject: 'Group',
      participants: [
        { id: ADMIN, admin: 'admin' },
        { id: MEMBER, admin: null },
      ],
    }),
  } as any;
}

beforeEach(() => clearGroupCache());

describe('looksLikeCommand', () => {
  it('detects a slash command after leading mentions', () => {
    expect(looksLikeCommand('/clean all')).toBe(true);
    expect(looksLikeCommand('@bot /broadcast off')).toBe(true);
    expect(looksLikeCommand('@bot stop the announcements')).toBe(false);
  });
});

describe('resolveSenderGroupAdmin', () => {
  it('resolves an admin for a plain addressed request (agent tool path)', async () => {
    const sock = makeSock();
    await expect(
      resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, '@bot stop the updates')
    ).resolves.toBe(true);
    await expect(
      resolveSenderGroupAdmin(sock, GROUP, { participant: MEMBER }, '@bot stop the updates')
    ).resolves.toBe(false);
  });

  it('serves plain requests from the metadata cache', async () => {
    const sock = makeSock();
    await resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, 'hello');
    await resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, 'hello again');
    expect(sock.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('fetches live metadata for every slash command', async () => {
    const sock = makeSock();
    await resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, '/clean all');
    await resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, '/clean all');
    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });

  it('returns undefined (not admin, fail closed) when the lookup fails', async () => {
    const sock = makeSock();
    sock.groupMetadata.mockRejectedValue(new Error('timeout'));
    await expect(
      resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, '/broadcast off')
    ).resolves.toBeUndefined();
    await expect(
      resolveSenderGroupAdmin(sock, GROUP, { participant: ADMIN }, 'stop the updates')
    ).resolves.toBeUndefined();
  });
});
