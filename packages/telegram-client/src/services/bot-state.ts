let ready = false;

export function markBotReady(): void {
  ready = true;
}

/**
 * Mark the bot as no longer serving (polling mode: the poll loop died).
 *
 * Readiness was a one-way latch set once at boot, so a dead poll loop would
 * keep /health reporting `healthy` through a total outage.
 */
export function markBotDisconnected(): void {
  ready = false;
}

export function isBotReady(): boolean {
  return ready;
}

/** Test-only helper: resets readiness. Safe to call from production too. */
export function _resetBotReadyForTests(): void {
  ready = false;
}
