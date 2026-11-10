/**
 * The "@bot, then the question" window. WhatsApp voice notes cannot carry a
 * mention, so a sender who types `@bot` and then records the question would
 * otherwise have the voice note saved as chatter and never answered. After a
 * message mentions the bot, the SAME sender's NEXT message in the same group,
 * within `ttlMs`, counts as addressed. One-shot: consuming it closes it.
 *
 * It only turns a save-only message into an addressed one; callers still run
 * the whitelist gate first, and its `suppressResponse` still wins.
 *
 * Pure (time is passed in) and dependency-free so the copy in telegram-client
 * stays byte-identical (enforced by whatsapp-client
 * `tests/unit/whitelist-copies.test.ts`). Fix it here, then `cp` it over.
 */

export interface MentionWindow {
  /** Open (or re-open) the window for `key` at time `now` (ms). */
  arm(key: string, now: number): void;
  /** True, and closes the window, if `key` has an open window at `now`. */
  consume(key: string, now: number): boolean;
  /** Number of tracked keys (tests and diagnostics). */
  size(): number;
}

/**
 * @param ttlMs       window length; `<= 0` disables it (arm is a no-op)
 * @param maxEntries  memory bound; past it the oldest entries are dropped
 */
export function createMentionWindow(ttlMs: number, maxEntries = 1000): MentionWindow {
  // Map keeps insertion order, so the first keys are the oldest arms.
  const expiries = new Map<string, number>();

  function prune(now: number): void {
    for (const [key, expiry] of expiries) {
      if (expiry <= now) expiries.delete(key);
    }
    while (expiries.size > maxEntries) {
      const oldest = expiries.keys().next().value;
      if (oldest === undefined) break;
      expiries.delete(oldest);
    }
  }

  return {
    arm(key, now) {
      if (ttlMs <= 0) return;
      expiries.delete(key); // re-insert so it counts as the newest
      expiries.set(key, now + ttlMs);
      prune(now);
    },
    consume(key, now) {
      const expiry = expiries.get(key);
      if (expiry === undefined) return false;
      expiries.delete(key);
      return expiry > now;
    },
    size() {
      return expiries.size;
    },
  };
}
