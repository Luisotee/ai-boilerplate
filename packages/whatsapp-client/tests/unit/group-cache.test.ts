import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock the logger module to prevent config/pino initialization errors
vi.mock('../../src/logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    trace: vi.fn(),
  },
}));

import {
  clearGroupCache,
  getGroupMetadataCached,
  getGroupSubject,
  invalidateGroup,
  peekGroupMetadata,
} from '../../src/services/group-cache.js';

const GROUP = '120363012345678@g.us';

function makeSock(subject = 'Test Group') {
  return {
    groupMetadata: vi.fn().mockResolvedValue({ id: GROUP, subject, participants: [] }),
  } as any;
}

beforeEach(() => {
  clearGroupCache();
  vi.useRealTimers();
});

afterEach(() => {
  vi.useRealTimers();
  clearGroupCache();
});

describe('getGroupMetadataCached', () => {
  it('fetches once and serves the cached value afterwards', async () => {
    const sock = makeSock();

    expect((await getGroupMetadataCached(sock, GROUP))?.subject).toBe('Test Group');
    expect((await getGroupMetadataCached(sock, GROUP))?.subject).toBe('Test Group');

    expect(sock.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('refetches once the TTL has elapsed', async () => {
    vi.useFakeTimers();
    const sock = makeSock();

    await getGroupMetadataCached(sock, GROUP);
    vi.advanceTimersByTime(5 * 60_000 + 1);
    await getGroupMetadataCached(sock, GROUP);

    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });

  it('coalesces concurrent lookups into a single network call', async () => {
    const sock = makeSock();

    await Promise.all([
      getGroupMetadataCached(sock, GROUP),
      getGroupMetadataCached(sock, GROUP),
      getGroupMetadataCached(sock, GROUP),
    ]);

    expect(sock.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('returns undefined instead of throwing when the query fails', async () => {
    const sock = { groupMetadata: vi.fn().mockRejectedValue(new Error('not a participant')) } as any;

    await expect(getGroupMetadataCached(sock, GROUP)).resolves.toBeUndefined();
  });

  it('negative-caches a failure so a backlog cannot hammer the network', async () => {
    const sock = { groupMetadata: vi.fn().mockRejectedValue(new Error('not a participant')) } as any;

    await getGroupMetadataCached(sock, GROUP);
    await getGroupMetadataCached(sock, GROUP);

    expect(sock.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('retries after the (shorter) failure TTL expires', async () => {
    vi.useFakeTimers();
    const sock = { groupMetadata: vi.fn().mockRejectedValue(new Error('transient')) } as any;

    await getGroupMetadataCached(sock, GROUP);
    vi.advanceTimersByTime(30_000 + 1);
    await getGroupMetadataCached(sock, GROUP);

    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });

  it('does not poison the cache — a later success replaces a cached failure', async () => {
    vi.useFakeTimers();
    const sock = {
      groupMetadata: vi
        .fn()
        .mockRejectedValueOnce(new Error('transient'))
        .mockResolvedValue({ id: GROUP, subject: 'Recovered', participants: [] }),
    } as any;

    expect(await getGroupMetadataCached(sock, GROUP)).toBeUndefined();
    vi.advanceTimersByTime(30_000 + 1);

    expect((await getGroupMetadataCached(sock, GROUP))?.subject).toBe('Recovered');
  });
});

describe('getGroupSubject', () => {
  it('returns the subject', async () => {
    expect(await getGroupSubject(makeSock('Equipe Terra Krya'), GROUP)).toBe('Equipe Terra Krya');
  });

  it('returns undefined for an empty subject rather than an empty name', async () => {
    expect(await getGroupSubject(makeSock(''), GROUP)).toBeUndefined();
  });

  it('returns undefined when the metadata query fails', async () => {
    const sock = { groupMetadata: vi.fn().mockRejectedValue(new Error('boom')) } as any;

    expect(await getGroupSubject(sock, GROUP)).toBeUndefined();
  });
});

describe('invalidateGroup', () => {
  it('forces the next lookup to refetch (a renamed group must not serve a stale subject)', async () => {
    const sock = {
      groupMetadata: vi
        .fn()
        .mockResolvedValueOnce({ id: GROUP, subject: 'Old Name', participants: [] })
        .mockResolvedValue({ id: GROUP, subject: 'New Name', participants: [] }),
    } as any;

    expect(await getGroupSubject(sock, GROUP)).toBe('Old Name');
    invalidateGroup(GROUP);
    expect(await getGroupSubject(sock, GROUP)).toBe('New Name');
    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });

  it('leaves other groups cached', async () => {
    const other = '120363099999999@g.us';
    const sock = makeSock();

    await getGroupMetadataCached(sock, GROUP);
    await getGroupMetadataCached(sock, other);
    invalidateGroup(GROUP);
    await getGroupMetadataCached(sock, other);

    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });
});

describe('peekGroupMetadata', () => {
  // Wired to Baileys' `cachedGroupMetadata`: it must never trigger a fetch, or a
  // network call lands inside Baileys' own send path.
  it('returns undefined on a cold cache without fetching', async () => {
    expect(await peekGroupMetadata(GROUP)).toBeUndefined();
  });

  it('returns the value once something else has populated it', async () => {
    const sock = makeSock();
    await getGroupMetadataCached(sock, GROUP);

    expect((await peekGroupMetadata(GROUP))?.subject).toBe('Test Group');
    expect(sock.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('returns undefined for a negatively-cached group', async () => {
    const sock = { groupMetadata: vi.fn().mockRejectedValue(new Error('boom')) } as any;
    await getGroupMetadataCached(sock, GROUP);

    expect(await peekGroupMetadata(GROUP)).toBeUndefined();
  });
});

describe('clearGroupCache', () => {
  it('drops everything so a relinked socket cannot serve another account’s groups', async () => {
    const sock = makeSock();

    await getGroupMetadataCached(sock, GROUP);
    clearGroupCache();
    expect(await peekGroupMetadata(GROUP)).toBeUndefined();

    await getGroupMetadataCached(sock, GROUP);
    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });
});
