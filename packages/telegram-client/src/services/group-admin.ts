import { bot, type TelegramContext } from '../bot.js';
import { logger } from '../logger.js';

/**
 * Whether the sender of this update is an admin of the group it came from.
 *
 * The AI API restricts `/clean`, `/tts`, `/stt`, `/settings` and `/memories` in
 * groups to admins and FAILS CLOSED: anything other than an explicit `true` is
 * refused. So this must return a real boolean — omitting the field would lock
 * genuine admins out of every admin command.
 *
 * **Fails closed.** If `getChatMember` errors (network, bot removed from the
 * group, rate limit) we report "not an admin" rather than letting a destructive
 * command through on an unverified claim. The cost of a false negative is an
 * admin being told to retry; the cost of a false positive is a wiped group
 * transcript.
 *
 * Callers should invoke this lazily — only for group messages addressed to the
 * bot (commands, and plain requests an agent tool may act on) — so un-addressed
 * chatter costs no Bot API call.
 */
export async function isSenderGroupAdmin(ctx: TelegramContext): Promise<boolean> {
  const chatId = ctx.chat?.id;
  const userId = ctx.from?.id;
  if (chatId === undefined || userId === undefined) return false;

  try {
    const member = await bot.api.getChatMember(chatId, userId);
    return member.status === 'creator' || member.status === 'administrator';
  } catch (err) {
    logger.warn(
      { err, chatId, userId },
      'Could not resolve group admin status — treating sender as non-admin'
    );
    return false;
  }
}
