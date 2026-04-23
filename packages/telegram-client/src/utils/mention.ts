import type { Message, MessageEntity } from 'grammy/types';

export interface BotIdentity {
  id: number;
  username: string;
}

/**
 * A group message is considered "addressed to the bot" when any of:
 *   - it is a reply to one of the bot's own messages
 *   - `entities` contains a `text_mention` for the bot's user.id
 *   - `entities` contains a `mention` whose substring (skipping the leading `@`)
 *     equals the bot's username
 */
export function isAddressedToBot(message: Message, bot: BotIdentity): boolean {
  if (message.reply_to_message?.from?.id === bot.id) return true;

  const text = message.text ?? message.caption ?? '';
  const entities: MessageEntity[] = message.entities ?? message.caption_entities ?? [];
  if (!text || entities.length === 0) return false;

  return entities.some((entity) => {
    if (entity.type === 'text_mention' && entity.user?.id === bot.id) return true;
    if (entity.type === 'mention') {
      const slice = text.slice(entity.offset + 1, entity.offset + entity.length);
      return slice === bot.username;
    }
    return false;
  });
}

/**
 * Remove a leading `@<bot_username>` mention from the message text so the AI
 * doesn't get a stray mention at the start of every group prompt.
 */
export function stripBotMention(text: string, bot: BotIdentity): string {
  const pattern = new RegExp(`^@${bot.username}\\s*`, 'i');
  return text.replace(pattern, '').trim();
}
