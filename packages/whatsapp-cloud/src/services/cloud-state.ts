/**
 * Simple ready-state singleton for the Cloud API connection.
 * Unlike Baileys (persistent WebSocket), the Cloud API is stateless HTTP,
 * so "ready" means the config has been validated and the API is accepting requests.
 */

let cloudApiReady = false;

export function setCloudApiReady(ready: boolean): void {
  cloudApiReady = ready;
}

export function isCloudApiConnected(): boolean {
  return cloudApiReady;
}
