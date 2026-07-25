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
    const sock = {
      groupMetadata: vi.fn().mockRejectedValue(new Error('not a participant')),
    } as any;

    await expect(getGroupMetadataCached(sock, GROUP)).resolves.toBeUndefined();
  });

  it('negative-caches a failure so a backlog cannot hammer the network', async () => {
    const sock = {
      groupMetadata: vi.fn().mockRejectedValue(new Error('not a participant')),
    } as any;

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

describe('clearGroupCache', () => {
  it('drops everything so a relinked socket cannot serve another account’s groups', async () => {
    const sock = makeSock();

    await getGroupMetadataCached(sock, GROUP);
    clearGroupCache();
    await getGroupMetadataCached(sock, GROUP);

    expect(sock.groupMetadata).toHaveBeenCalledTimes(2);
  });
});

describe('stale writes and stuck lookups', () => {
  // Regressions for two bugs found in review. Both are timing-dependent, so
  // they need a fetch held open rather than a plain mockResolvedValue.
  function gatedSock() {
    let release!: (v: unknown) => void;
    const gate = new Promise((r) => {
      release = r;
    });
    return { sock: { groupMetadata: vi.fn().mockReturnValue(gate) } as any, release };
  }

  it('clearGroupCache during an in-flight fetch does not repopulate', async () => {
    const { sock, release } = gatedSock();

    const pending = getGroupMetadataCached(sock, GROUP);
    clearGroupCache(); // e.g. logoutWhatsApp, mid-fetch
    release({ id: GROUP, subject: 'Previous account group', participants: [] });
    await pending;

    // A relink must not inherit the previous account's groups: the next read
    // has to go back to the network rather than serve the resolved-late write.
    const fresh = {
      groupMetadata: vi.fn().mockResolvedValue({ id: GROUP, subject: 'New', participants: [] }),
    } as any;
    expect((await getGroupMetadataCached(fresh, GROUP))?.subject).toBe('New');
    expect(fresh.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('invalidateGroup during an in-flight fetch does not reinstall the stale subject', async () => {
    const { sock, release } = gatedSock();

    const pending = getGroupMetadataCached(sock, GROUP);
    invalidateGroup(GROUP); // groups.update (rename) arrives mid-fetch
    release({ id: GROUP, subject: 'Old Name', participants: [] });
    await pending;

    const fresh = {
      groupMetadata: vi
        .fn()
        .mockResolvedValue({ id: GROUP, subject: 'New Name', participants: [] }),
    } as any;
    expect(await getGroupSubject(fresh, GROUP)).toBe('New Name');
  });

  it('a synchronous throw does not strand an inflight entry forever', async () => {
    // An async body runs synchronously up to its first await, so a sync throw
    // used to delete the inflight entry before it was even added — leaving a
    // resolved-undefined promise that every later call would be served.
    const broken = {} as any; // groupMetadata undefined -> synchronous TypeError
    expect(await getGroupMetadataCached(broken, GROUP)).toBeUndefined();

    // Clear the negative entry so this asserts only the inflight map, not the
    // failure TTL (which has its own test above).
    invalidateGroup(GROUP);

    const good = {
      groupMetadata: vi
        .fn()
        .mockResolvedValue({ id: GROUP, subject: 'Recovered', participants: [] }),
    } as any;
    expect((await getGroupMetadataCached(good, GROUP))?.subject).toBe('Recovered');
    expect(good.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it('getGroupSubject gives up rather than stalling the message hot path', async () => {
    vi.useFakeTimers();
    const { sock } = gatedSock(); // never released

    const pending = getGroupSubject(sock, GROUP);
    await vi.advanceTimersByTimeAsync(3_000 + 1);

    expect(await pending).toBeUndefined();
    vi.useRealTimers();
  });
});
