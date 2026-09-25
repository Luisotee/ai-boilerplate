/**
 * Unit tests for services/groups.ts — shared-group resolution and the
 * GROUP_GATING=membership "has a whitelisted member" check.
 *
 * Both reuse the whitelist matcher, so these pin the namespace rules too: a
 * LID's digits never match a phone, and vice versa.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), trace: vi.fn() },
}));

import { findSharedGroups, groupHasWhitelistedMember } from '../../src/services/groups.js';
import { clearGroupCache } from '../../src/services/group-cache.js';
import { parseWhitelist } from '../../src/utils/whitelist.js';

type Participant = { id: string; lid?: string; phoneNumber?: string };
type Groups = Record<string, { id?: string; subject: string; participants: Participant[] }>;

function makeSock(groups: Groups, getPNForLID = vi.fn().mockResolvedValue(null)) {
  const withIds = Object.fromEntries(
    Object.entries(groups).map(([jid, g]) => [jid, { id: jid, ...g }])
  );
  return {
    groupFetchAllParticipating: vi.fn().mockResolvedValue(withIds),
    groupMetadata: vi.fn(async (jid: string) => {
      const meta = withIds[jid];
      if (!meta) throw new Error('item-not-found');
      return meta;
    }),
    signalRepository: { lidMapping: { getPNForLID } },
  } as any;
}

const G1 = '120363000000001@g.us';
const G2 = '120363000000002@g.us';
const G3 = '120363000000003@g.us';

beforeEach(() => {
  vi.clearAllMocks();
  clearGroupCache();
});

describe('findSharedGroups', () => {
  it('matches a phone-JID participant (device suffix tolerated)', async () => {
    const sock = makeSock({
      [G1]: {
        subject: 'Book Club',
        participants: [
          { id: '5511000000000@s.whatsapp.net' },
          { id: '5511777777777:12@s.whatsapp.net' },
        ],
      },
    });
    expect(await findSharedGroups(sock, { jid: '5511777777777@s.whatsapp.net' })).toEqual([
      { groupJid: G1, subject: 'Book Club' },
    ]);
  });

  it('matches by E.164 phone against participant.phoneNumber', async () => {
    const sock = makeSock({
      [G1]: {
        subject: 'Book Club',
        participants: [{ id: '424242@lid', phoneNumber: '5511777777777@s.whatsapp.net' }],
      },
    });
    const result = await findSharedGroups(sock, { phone: '+5511777777777' });
    expect(result.map((g) => g.groupJid)).toEqual([G1]);
  });

  it('matches a LID-addressed participant via its lid', async () => {
    const sock = makeSock({
      [G2]: { subject: 'Hiking', participants: [{ id: '999888@lid', lid: '999888@lid' }] },
    });
    const result = await findSharedGroups(sock, { lid: '999888@lid' });
    expect(result.map((g) => g.groupJid)).toEqual([G2]);
  });

  it('falls back to getPNForLID for a LID-only participant', async () => {
    const getPNForLID = vi.fn().mockResolvedValue('5511555555555:0@s.whatsapp.net');
    const sock = makeSock(
      { [G3]: { subject: 'Choir', participants: [{ id: '424242@lid' }] } },
      getPNForLID
    );
    const result = await findSharedGroups(sock, { jid: '5511555555555@s.whatsapp.net' });
    expect(result.map((g) => g.groupJid)).toEqual([G3]);
    expect(getPNForLID).toHaveBeenCalledWith('424242@lid');
  });

  it('does not match when getPNForLID returns null or throws', async () => {
    const sock = makeSock(
      { [G3]: { subject: 'Choir', participants: [{ id: '424242@lid' }, { id: '1@lid' }] } },
      vi.fn().mockResolvedValueOnce(null).mockRejectedValueOnce(new Error('store down'))
    );
    expect(await findSharedGroups(sock, { jid: '5511555555555@s.whatsapp.net' })).toEqual([]);
  });

  it('returns exactly the subset of groups the requester belongs to', async () => {
    const me = '5511777777777@s.whatsapp.net';
    const sock = makeSock({
      [G1]: { subject: 'A', participants: [{ id: me }] },
      [G2]: { subject: 'B', participants: [{ id: '5511000000000@s.whatsapp.net' }] },
      [G3]: { subject: 'C', participants: [{ id: me }, { id: '5511000000000@s.whatsapp.net' }] },
    });
    const result = await findSharedGroups(sock, { jid: me });
    expect(result.map((g) => g.subject)).toEqual(['A', 'C']);
  });

  it('PRIVACY: a LID participant whose digits equal the requester phone does not match', async () => {
    const sock = makeSock({
      [G1]: { subject: 'A', participants: [{ id: '5511777777777@lid' }] },
    });
    expect(
      await findSharedGroups(sock, { jid: '5511777777777@s.whatsapp.net', phone: '+5511777777777' })
    ).toEqual([]);
  });

  it('PRIVACY: a phone participant whose digits equal the requester LID does not match', async () => {
    const sock = makeSock({
      [G1]: { subject: 'A', participants: [{ id: '999888@s.whatsapp.net' }] },
    });
    expect(await findSharedGroups(sock, { lid: '999888@lid' })).toEqual([]);
  });

  it('does not match a phone off by one digit', async () => {
    const sock = makeSock({
      [G1]: { subject: 'A', participants: [{ id: '5511777777778@s.whatsapp.net' }] },
    });
    expect(await findSharedGroups(sock, { jid: '5511777777777@s.whatsapp.net' })).toEqual([]);
  });

  it('returns [] without querying when no identifier is supplied', async () => {
    const sock = makeSock({ [G1]: { subject: 'A', participants: [] } });
    expect(await findSharedGroups(sock, {})).toEqual([]);
    expect(sock.groupFetchAllParticipating).not.toHaveBeenCalled();
  });

  it('propagates a failed fleet query (the route answers 500, not "no groups")', async () => {
    const sock = makeSock({});
    sock.groupFetchAllParticipating.mockRejectedValueOnce(new Error('timeout'));
    await expect(findSharedGroups(sock, { jid: '1@s.whatsapp.net' })).rejects.toThrow('timeout');
  });
});

describe('groupHasWhitelistedMember', () => {
  it('returns true for a disabled (empty) whitelist without fetching', async () => {
    const sock = makeSock({});
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist(''))).toBe(true);
    expect(sock.groupMetadata).not.toHaveBeenCalled();
  });

  it('matches a phone-JID participant against a bare-digit entry', async () => {
    const sock = makeSock({
      [G1]: { subject: 'A', participants: [{ id: '5511777777777:3@s.whatsapp.net' }] },
    });
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('+55 11 77777-7777'))).toBe(
      true
    );
  });

  it('matches a LID participant through participant.phoneNumber', async () => {
    const sock = makeSock({
      [G1]: {
        subject: 'A',
        participants: [{ id: '424242@lid', phoneNumber: '5511777777777@s.whatsapp.net' }],
      },
    });
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('5511777777777'))).toBe(true);
  });

  it('matches a LID participant via the getPNForLID fallback', async () => {
    const sock = makeSock(
      { [G1]: { subject: 'A', participants: [{ id: '424242@lid' }] } },
      vi.fn().mockResolvedValue('5511777777777:0@s.whatsapp.net')
    );
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('5511777777777'))).toBe(true);
  });

  it('matches a verbatim LID entry', async () => {
    const sock = makeSock({ [G1]: { subject: 'A', participants: [{ id: '424242@lid' }] } });
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('424242@lid'))).toBe(true);
  });

  it('PRIVACY: a phone-shaped entry never matches a LID with the same digits', async () => {
    const sock = makeSock({ [G1]: { subject: 'A', participants: [{ id: '5511777777777@lid' }] } });
    for (const entry of ['+5511777777777', '5511777777777@s.whatsapp.net']) {
      expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist(entry))).toBe(false);
    }
  });

  it('a BARE entry is namespace-blind, exactly like the whitelist matcher itself', async () => {
    // Same legacy clause that lets a bare LID admit its chat (pinned in
    // whitelist.test.ts) — membership deliberately does not diverge from it.
    const sock = makeSock({ [G1]: { subject: 'A', participants: [{ id: '5511777777777@lid' }] } });
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('5511777777777'))).toBe(true);
  });

  it('returns false when no participant is whitelisted', async () => {
    const sock = makeSock({
      [G1]: { subject: 'A', participants: [{ id: '5511000000000@s.whatsapp.net' }] },
    });
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('5511777777777'))).toBe(false);
  });

  it('fails closed when the metadata lookup fails', async () => {
    const sock = makeSock({});
    expect(await groupHasWhitelistedMember(sock, G1, parseWhitelist('5511777777777'))).toBe(false);
  });
});
