import type { Message, MessageEntity } from 'grammy/types';
import { isTypedMention, normalizeMentionNames } from './typed-mention.js';

export interface BotIdentity {
  id: number;
  username: string;
  /**
   * Names a typed `@Name` may use for the bot (BOT_NAME, its first_name). The
   * username itself is covered by the `mention` entity check.
   */
  typedNames?: readonly (string | undefined | null)[];
}

/** How a group message addresses the bot, if it does. */
export type Addressing = 'mention' | 'reply' | 'command' | null;

/**
 * A group message is considered "addressed to the bot" when any of:
 *   - it is a reply to one of the bot's own messages
 *   - `entities` contains a `text_mention` for the bot's user.id
 *   - `entities` contains a `mention` whose substring (skipping the leading `@`)
 *     equals the bot's username
 *   - it opens with a `bot_command` that is either unqualified (`/settings`) or
 *     qualified with our username (`/settings@MyBot`)
 *   - it contains a typed `@Name` for one of `bot.typedNames` and no mention of
 *     anyone else (see `addressingOf`)
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
  return addressingOf(message, bot) !== null;
}

/**
 * Like `isAddressedToBot`, but says HOW: `'mention'` (an @username / text
 * mention, or a typed `@Name`), `'reply'` or `'command'`. Only a mention opens
 * the "@bot, then the question" window (utils/mention-window.ts).
 *
 * Typed `@Name`: Telegram tags any `@word` that looks like a username as a
 * `mention` entity, whether or not it is one, so `@Curupira` for a bot named
 * `@curupira_bot` arrives as an unrelated mention. It counts when no entity
 * points at someone else: no `text_mention` for another user, and every
 * `mention` entity is one of the typed names.
 */
export function addressingOf(message: Message, bot: BotIdentity): Addressing {
  if (message.reply_to_message?.from?.id === bot.id) return 'reply';

  const text = message.text ?? message.caption ?? '';
  const entities: MessageEntity[] = message.entities ?? message.caption_entities ?? [];
  if (!text) return null;

  const username = bot.username.toLowerCase();
  let command = false;
  let mention = false;

  for (const entity of entities) {
    if (entity.type === 'text_mention' && entity.user?.id === bot.id) mention = true;

    if (entity.type === 'mention') {
      const slice = text.slice(entity.offset + 1, entity.offset + entity.length);
      if (slice.toLowerCase() === username) mention = true;
    }

    if (entity.type === 'bot_command' && entity.offset === 0) {
      const cmd = text.slice(entity.offset, entity.offset + entity.length);
      const at = cmd.indexOf('@');
      // Unqualified `/foo` in a group is for us; `/foo@other` is not.
      if (at === -1 || cmd.slice(at + 1).toLowerCase() === username) command = true;
    }
  }

  if (mention) return 'mention';
  if (command) return 'command';
  if (isTypedBotMention(text, entities, bot)) return 'mention';
  return null;
}

function isTypedBotMention(text: string, entities: MessageEntity[], bot: BotIdentity): boolean {
  const names = normalizeMentionNames(bot.typedNames ?? []);
  if (names.length === 0) return false;
  const lowered = new Set(names.map((n) => n.toLowerCase()));
  const pointsElsewhere = entities.some((entity) => {
    if (entity.type === 'text_mention') return entity.user?.id !== bot.id;
    if (entity.type === 'mention') {
      const slice = text.slice(entity.offset + 1, entity.offset + entity.length);
      return !lowered.has(slice.toLowerCase());
    }
    return false;
  });
  return !pointsElsewhere && isTypedMention(text, names);
}

/**
 * Remove a leading `@<bot_username>` mention from the message text so the AI
 * doesn't get a stray mention at the start of every group prompt.
 */
export function stripBotMention(text: string, bot: BotIdentity): string {
  const pattern = new RegExp(`^@${bot.username}\\s*`, 'i');
  return text.replace(pattern, '').trim();
}
