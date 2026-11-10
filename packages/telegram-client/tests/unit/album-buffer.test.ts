import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { collectAlbumItem, pendingAlbumCount } from '../../src/utils/album-buffer.js';

const opts = { quietMs: 100, maxWaitMs: 1000, maxItems: 10 };

describe('collectAlbumItem', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('resolves the first item with the whole album once it goes quiet', async () => {
    const leader = collectAlbumItem('a', 1, opts);
    await expect(collectAlbumItem('a', 2, opts)).resolves.toBeNull();
    await vi.advanceTimersByTimeAsync(50);
    await expect(collectAlbumItem('a', 3, opts)).resolves.toBeNull();

    await vi.advanceTimersByTimeAsync(99);
    expect(pendingAlbumCount()).toBe(1); // each photo restarts the quiet period
    await vi.advanceTimersByTimeAsync(1);

    await expect(leader).resolves.toEqual([1, 2, 3]);
    expect(pendingAlbumCount()).toBe(0);
  });

  it('never waits longer than maxWaitMs in total', async () => {
    const leader = collectAlbumItem('b', 0, { ...opts, maxWaitMs: 250 });
    for (let i = 1; i <= 5; i++) {
      await vi.advanceTimersByTimeAsync(60);
      void collectAlbumItem('b', i, { ...opts, maxWaitMs: 250 });
    }
    await vi.advanceTimersByTimeAsync(0);
    await expect(leader).resolves.toHaveLength(5);
  });

  it('flushes at maxItems', async () => {
    const leader = collectAlbumItem('c', 1, { ...opts, maxItems: 2 });
    await collectAlbumItem('c', 2, { ...opts, maxItems: 2 });
    await expect(leader).resolves.toEqual([1, 2]);
  });

  it('keeps different albums apart', async () => {
    const one = collectAlbumItem('x', 'x1', opts);
    const two = collectAlbumItem('y', 'y1', opts);
    await vi.advanceTimersByTimeAsync(100);
    await expect(one).resolves.toEqual(['x1']);
    await expect(two).resolves.toEqual(['y1']);
  });

  it('passes every photo straight through when grouping is off', async () => {
    const before = pendingAlbumCount();
    await expect(collectAlbumItem('d', 1, { ...opts, quietMs: 0 })).resolves.toEqual([1]);
    expect(pendingAlbumCount()).toBe(before);
  });
});
