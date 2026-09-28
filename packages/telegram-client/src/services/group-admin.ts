import { bot, type TelegramContext } from '../bot.js';
import { logger } from '../logger.js';

/** Longest an admin lookup may hold up a reply; a timeout counts as not-admin. */
export const ADMIN_LOOKUP_TIMEOUT_MS = 3_000;

/**
 * Whether the sender of this update is an admin of the group it came from.
 * Bounded by `ADMIN_LOOKUP_TIMEOUT_MS` (a timeout is "not an admin").
 *
 * The AI API restricts `/clean`, `/tts`, `/stt`, `/settings`, `/memories` and
 * `/broadcast` (and the group-changing agent tools) in groups to admins and
 * FAILS CLOSED: anything other than an explicit `true` is refused. So this
 * must return a real boolean — omitting the field would lock genuine admins
 * out of every admin command.
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
  // An anonymous admin posts AS the group: `sender_chat` is the group itself
  // and `from` is the fake @GroupAnonymousBot, so getChatMember can't tell who
  // it is. Posting as the group is an admin-only right (`is_anonymous`), so
  // that alone proves admin status. A linked-channel auto-forward or a user
  // posting as their own channel carries a DIFFERENT sender_chat and falls
  // through — and then to "not admin", since `from` is a fake user there too.
  if (chatId !== undefined && ctx.msg?.sender_chat?.id === chatId) return true;
  const userId = ctx.from?.id;
  if (chatId === undefined || userId === undefined) return false;

  let timer: NodeJS.Timeout | undefined;
  try {
    const timeout = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), ADMIN_LOOKUP_TIMEOUT_MS);
    });
    const member = await Promise.race([bot.api.getChatMember(chatId, userId), timeout]);
    if (member === null) {
      logger.warn(
        { chatId, userId },
        'Group admin lookup timed out — treating sender as non-admin'
      );
      return false;
    }
    return member.status === 'creator' || member.status === 'administrator';
  } catch (err) {
    logger.warn(
      { err, chatId, userId },
      'Could not resolve group admin status — treating sender as non-admin'
    );
    return false;
  } finally {
    clearTimeout(timer);
  }
}
