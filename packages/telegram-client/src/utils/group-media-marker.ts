/**
 * Build a `[Image: ...]` / `[Document: ...]` save-only marker for group
 * messages where the bot wasn't addressed. Mirrors the markers Baileys
 * produces in whatsapp-client/src/whatsapp.ts:226-273 so the AI sees
 * consistent group history across both clients.
 */
export function imageMarker(caption: string | null | undefined): string {
  return caption ? `[Image: ${caption}]` : '[Image]';
}

export function documentMarker(filename: string, caption: string | null | undefined): string {
  return caption ? `[Document: ${filename}] - ${caption}` : `[Document: ${filename}]`;
}
