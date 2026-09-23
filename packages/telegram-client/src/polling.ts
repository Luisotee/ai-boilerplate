/**
 * TELEGRAM_MODE=polling: long polling via @grammyjs/runner.
 *
 * Kept out of main.ts (which starts the server on import) so it can be tested.
 * No public URL or tunnel is needed in this mode, and no /webhook route is
 * registered.
 */

import { run, type RunnerHandle } from '@grammyjs/runner';
import { bot } from './bot.js';
import { config } from './config.js';

interface DrainLog {
  info: (obj: object, msg: string) => void;
  warn: (obj: object, msg: string) => void;
}

/**
 * Clear any registered webhook and start polling. Returns immediately — the
 * handle owns the poll loop; do NOT await `run()`.
 *
 * `deleteWebhook` first: while a webhook is registered every `getUpdates` fails
 * with 409 Conflict, permanently. It is idempotent and cheap, so it runs
 * unconditionally (switching webhook → polling needs no manual step). It does
 * NOT protect against a second process polling the same token — use a separate
 * bot token per environment.
 *
 * The runner processes updates CONCURRENTLY (grammY's built-in `bot.start()`
 * is strictly sequential, so one reply waiting on the AI API for up to
 * POLL_MAX_DURATION_MS would stall every other chat); per-chat ordering is
 * restored by `sequentialize` in bot.ts.
 */
export async function startLongPolling(): Promise<RunnerHandle> {
  await bot.api.deleteWebhook({ drop_pending_updates: config.telegram.dropPendingUpdates });
  return run(bot, {
    runner: {
      fetch: {
        allowed_updates: ['message'],
        timeout: config.telegram.pollTimeoutSeconds,
      },
      // The runner's own default is 15 HOURS of exponential backoff before it
      // gives up. Bound it so a partition surfaces promptly and the process can
      // exit for a restart. (Only effective because bot.ts rethrows HttpErrors
      // in polling mode — otherwise auto-retry swallows them below this layer.)
      maxRetryTime: config.telegram.maxPollRetryMs,
    },
    sink: { concurrency: config.telegram.concurrency },
  });
}

/**
 * Wait for handlers still running after `runner.stop()`.
 *
 * `runner.stop()` does NOT wait for in-flight middleware, despite its JSDoc: it
 * returns the poll-loop promise, which awaits the sink's `capacity()` — and
 * that resolves as soon as a slot frees up, not when work completes. Without
 * this drain every deploy would cut replies off mid-flight. `size()` is the
 * only in-flight counter RunnerHandle exposes.
 *
 * Bounded by `timeoutMs` so a wedged handler cannot block shutdown forever.
 */
export async function drainInFlight(
  runner: Pick<RunnerHandle, 'size'>,
  log: DrainLog,
  timeoutMs: number = config.telegram.shutdownDrainMs,
  pollMs = 100
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let pending = runner.size();
  if (pending === 0) return;

  log.info({ pending }, 'Waiting for in-flight updates to finish');
  while (pending > 0 && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollMs));
    pending = runner.size();
  }

  if (pending > 0) {
    log.warn({ pending, timeoutMs }, 'Drain timed out — abandoning in-flight updates');
  } else {
    log.info({}, 'All in-flight updates finished');
  }
}

/**
 * Stop polling, then drain. Order matters: stop pulling new updates first, let
 * running handlers finish, and only then may the caller close the HTTP server —
 * the AI API calls back into it (reactions, sends) while handlers run.
 */
export async function stopLongPolling(runner: RunnerHandle, log: DrainLog): Promise<void> {
  if (runner.isRunning()) await runner.stop();
  await drainInFlight(runner, log);
}
