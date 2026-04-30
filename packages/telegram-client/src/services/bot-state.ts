let ready = false;

export function markBotReady(): void {
  ready = true;
}

export function isBotReady(): boolean {
  return ready;
}

/** Test-only helper: resets readiness. Safe to call from production too. */
export function _resetBotReadyForTests(): void {
  ready = false;
}
