let ready = false;

export function markBotReady(): void {
  ready = true;
}

export function isBotReady(): boolean {
  return ready;
}
