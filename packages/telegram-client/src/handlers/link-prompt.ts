import { Keyboard } from 'grammy';
import type { TelegramContext } from '../bot.js';
import { linkPhone } from '../api-client.js';
import { logger } from '../logger.js';
import { chatIdToJid } from '../utils/telegram-id.js';

/**
 * Offer the one-tap account link (`/linkphone`).
 *
 * `request_contact` is only available in private chats (Telegram restricts
 * every `KeyboardButton` request_* variant that way), so a group caller is
 * pointed at a private chat instead.
 */
export async function offerPhoneLink(ctx: TelegramContext): Promise<void> {
  if (ctx.chat?.type !== 'private') {
    await ctx.reply('Account linking only works in a private chat. Message me directly to link.');
    return;
  }

  const keyboard = new Keyboard().requestContact('📱 Share my phone number').resized().oneTime();

  await ctx.reply(
    'To share memory with your WhatsApp conversation, share your phone number ' +
      'with the button below. It must be the same number you use on WhatsApp.\n\n' +
      "If you'd rather not share it, send `/link` on WhatsApp to get a code and " +
      'enter it here with `/link <code>`.\n\n' +
      'Note: your conversation history here on Telegram will be discarded when the ' +
      'accounts are linked.',
    { reply_markup: keyboard }
  );
}

/**
 * Handle a shared contact card.
 *
 * Everything security-relevant is decided server-side (`services/autolink.py`)
 * — in particular that `contact.user_id` must equal `ctx.from.id`, which is
 * what shows the number belongs to the sender rather than to someone in their
 * address book. Both ids are forwarded unmodified and the API refuses.
 */
export async function handleSharedContact(ctx: TelegramContext): Promise<void> {
  const contact = ctx.msg?.contact;
  const chatId = ctx.chat?.id;
  if (!contact || chatId === undefined) return;

  // Contact cards shared in groups are ordinary chatter, never a link request.
  if (ctx.chat?.type !== 'private') return;

  const jid = chatIdToJid(chatId);
  logger.info(
    { jid, hasContactUserId: contact.user_id !== undefined },
    'Received shared contact for account linking'
  );

  const reply = await linkPhone(jid, {
    phone: contact.phone_number,
    contactUserId: contact.user_id,
    senderUserId: ctx.from?.id,
  });

  await ctx.reply(reply || "I couldn't link your accounts right now. Please try again later.", {
    // Take the keyboard away whether or not it worked, so the button doesn't
    // linger over the chat.
    reply_markup: { remove_keyboard: true },
  });
}
