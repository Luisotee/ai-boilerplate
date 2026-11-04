let ready = false;
let botUsername: string | null = null;

export function markBotReady(username?: string | null): void {
  ready = true;
  if (username !== undefined) {
    botUsername = username;
  }
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

/**
 * Get the bot's Telegram handle (without '@').
 */
export function getBotUsername(): string | null {
  return botUsername;
}

/** Test-only helper: resets readiness and stored username. Safe to call from production too. */
export function _resetBotReadyForTests(): void {
  ready = false;
  botUsername = null;
}
