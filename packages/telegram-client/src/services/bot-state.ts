import { bot } from '../bot.js';

let ready = false;
let customUsername: string | null = null;

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

/**
 * Get the bot's Telegram handle (without '@').
 * Reads from bot.botInfo when initialized.
 */
export function getBotUsername(): string | null {
  if (customUsername !== null) {
    return customUsername;
  }
  try {
    if (bot.isInited() && bot.botInfo?.username) {
      return bot.botInfo.username;
    }
  } catch {
    // bot.botInfo throws when uninitialized in grammY
  }
  return null;
}

/** Test-only helper: override username for testing. */
export function setBotUsernameForTests(username: string | null): void {
  customUsername = username;
}

/** Test-only helper: resets readiness. Safe to call from production too. */
export function _resetBotReadyForTests(): void {
  ready = false;
  customUsername = null;
}
