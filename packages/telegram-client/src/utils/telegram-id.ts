/**
 * Translate between Telegram chat IDs (numeric) and the AI API's synthetic
 * JID format. Telegram supergroup/channel IDs are negative integers
 * (e.g. -1001234567890), so we render them verbatim.
 *
 * Format: "tg:<chat_id>"
 */
const JID_PREFIX = 'tg:';

export function chatIdToJid(chatId: number): string {
  return `${JID_PREFIX}${chatId}`;
}

export function jidToChatId(jid: string): number {
  if (!jid.startsWith(JID_PREFIX)) {
    throw new Error(`Not a Telegram JID: ${jid}`);
  }
  const raw = jid.slice(JID_PREFIX.length);
  const id = Number(raw);
  if (!Number.isFinite(id) || !Number.isInteger(id)) {
    throw new Error(`Invalid Telegram chat id: ${raw}`);
  }
  return id;
}

export function isTelegramJid(jid: string): boolean {
  return jid.startsWith(JID_PREFIX);
}

/**
 * Maps Telegram chat.type onto the AI API's conversation_type dimension.
 * `channel` maps to `group` for now — the bot receiving a channel message
 * behaves the same as a group context from the AI's perspective.
 */
export function chatTypeToConversationType(
  chatType: 'private' | 'group' | 'supergroup' | 'channel'
): 'private' | 'group' {
  return chatType === 'private' ? 'private' : 'group';
}
