/**
 * Group the photos of a Telegram album (updates sharing a `media_group_id`)
 * into one AI request.
 *
 * Telegram delivers an album as separate updates, one photo each. The first
 * update to arrive becomes the album's leader: its call resolves with every
 * item once no new one arrived for `quietMs` (or after `maxWaitMs`, or at
 * `maxItems`). Every later item's call resolves `null` at once, so its
 * handler just returns. Album updates must therefore run concurrently: `bot.ts`
 * exempts them from `sequentialize`, or the leader would wait for photos
 * that are queued behind it.
 */

interface PendingAlbum<T> {
  items: T[];
  startedAt: number;
  timer: ReturnType<typeof setTimeout>;
  resolve: (items: T[]) => void;
}

export interface AlbumOptions {
  quietMs: number;
  maxWaitMs: number;
  maxItems: number;
}

const pending = new Map<string, PendingAlbum<unknown>>();

export function collectAlbumItem<T>(
  key: string,
  item: T,
  options: AlbumOptions
): Promise<T[] | null> {
  if (options.quietMs <= 0) return Promise.resolve([item]);

  const existing = pending.get(key) as PendingAlbum<T> | undefined;
  if (existing) {
    existing.items.push(item);
    if (existing.items.length >= options.maxItems) {
      flush(key);
    } else {
      schedule(key, existing, options);
    }
    return Promise.resolve(null);
  }

  return new Promise<T[]>((resolve) => {
    const album: PendingAlbum<T> = {
      items: [item],
      startedAt: Date.now(),
      timer: undefined as unknown as ReturnType<typeof setTimeout>,
      resolve,
    };
    pending.set(key, album as PendingAlbum<unknown>);
    schedule(key, album, options);
  });
}

function schedule<T>(key: string, album: PendingAlbum<T>, options: AlbumOptions): void {
  clearTimeout(album.timer);
  const left = album.startedAt + options.maxWaitMs - Date.now();
  album.timer = setTimeout(() => flush(key), Math.max(0, Math.min(options.quietMs, left)));
}

function flush(key: string): void {
  const album = pending.get(key);
  if (!album) return;
  pending.delete(key);
  clearTimeout(album.timer);
  album.resolve(album.items);
}

/** Test helper: albums still waiting. */
export function pendingAlbumCount(): number {
  return pending.size;
}
