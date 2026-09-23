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
 *   - it opens with a `bot_command` that is either unqualified (`/settings`) or
 *     qualified with our username (`/settings@MyBot`)
 *
 * The command case matters more than it looks. Telegram emits `/settings@Bot`
 * as a SINGLE `bot_command` entity — there is no separate `mention` entity for
 * the suffix — so a mention-only check treats it as ordinary chatter and the
 * command is silently saved to history instead of being executed. That is the
 * form Telegram's own command menu produces in groups, and the only form a bot
 * reliably receives when privacy mode is on.
 *
 * Username comparison is case-insensitive throughout: Telegram usernames are
 * case-insensitive, so `@mybot` addresses `@MyBot`.
 */
export function isAddressedToBot(message: Message, bot: BotIdentity): boolean {
  if (message.reply_to_message?.from?.id === bot.id) return true;

  const text = message.text ?? message.caption ?? '';
  const entities: MessageEntity[] = message.entities ?? message.caption_entities ?? [];
  if (!text || entities.length === 0) return false;

  const username = bot.username.toLowerCase();

  return entities.some((entity) => {
    if (entity.type === 'text_mention' && entity.user?.id === bot.id) return true;

    if (entity.type === 'mention') {
      const slice = text.slice(entity.offset + 1, entity.offset + entity.length);
      return slice.toLowerCase() === username;
    }

    if (entity.type === 'bot_command' && entity.offset === 0) {
      const command = text.slice(entity.offset, entity.offset + entity.length);
      const at = command.indexOf('@');
      // Unqualified `/foo` in a group is for us; `/foo@other` is not.
      if (at === -1) return true;
      return command.slice(at + 1).toLowerCase() === username;
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
