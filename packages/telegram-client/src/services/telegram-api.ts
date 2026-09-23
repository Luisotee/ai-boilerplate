/**
 * Telegram Bot API wrapper used for outbound calls that originate OUTSIDE the
 * grammY update loop — e.g. the AI API calling POST /telegram/send-text to
 * deliver a response. Inside handlers, prefer ctx.reply / ctx.replyWithVoice /
 * ctx.react directly.
 */

import { GrammyError, InputFile } from 'grammy';
import type { ReactionTypeEmoji } from 'grammy/types';
import { bot } from '../bot.js';
import { config } from '../config.js';
import { logger } from '../logger.js';
import { fetchWithTimeout } from '../utils/fetch.js';
import { waMarkupToHtml } from '../utils/wa-markup-to-html.js';
import { splitByLength } from '../utils/message-split.js';
import { buildVCard } from '../utils/vcard-builder.js';

// ---------------------------------------------------------------------------
// Reaction emoji substitution
// ---------------------------------------------------------------------------
//
// Telegram's allowed standard-emoji reaction list (Bot API 7.x) does NOT
// include ⏳, ✅, or ❌ — the status emojis the WhatsApp clients use. Reacting
// with an unsupported emoji returns 400 BAD_REQUEST: REACTION_INVALID.
//
// See: https://core.telegram.org/bots/api#reactiontypeemoji
const REACTION_MAP: Record<string, string> = {
  '⏳': '🤔',
  '✅': '👍',
  '❌': '👎',
};

function substituteReaction(emoji: string): string {
  return REACTION_MAP[emoji] ?? emoji;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** True for the 400 Telegram returns when parse_mode entities don't validate. */
export function isParseEntitiesError(error: unknown): boolean {
  return (
    error instanceof GrammyError &&
    error.error_code === 400 &&
    /can't parse entities/i.test(error.description)
  );
}

/**
 * Send one already-length-bounded piece.
 *
 * A malformed entity rejects the whole message, so on `can't parse entities` we
 * retry once as plain text: delivering the response unformatted is far better
 * than dropping it. The converter is conservative enough that this should not
 * fire, and it logs loudly if it does.
 */
async function sendOne(
  chatId: number,
  text: string,
  replyParams: Record<string, unknown>
): Promise<number> {
  try {
    const message = await bot.api.sendMessage(chatId, waMarkupToHtml(text), {
      parse_mode: 'HTML',
      ...replyParams,
    });
    return message.message_id;
  } catch (error) {
    if (!isParseEntitiesError(error)) throw error;
    logger.warn(
      { err: error, chatId },
      'Telegram rejected HTML entities — retrying as plain text. ' +
        'This means waMarkupToHtml produced unbalanced markup; the input is worth investigating.'
    );
    const message = await bot.api.sendMessage(chatId, text, replyParams);
    return message.message_id;
  }
}

/**
 * Send `text`, translating WhatsApp markup to Telegram HTML.
 *
 * Splits the RAW text at Telegram's 4096-character limit before converting and
 * sending. This path carries the AI API's server-initiated pushes (e.g. the
 * agent's `send_whatsapp_message` tool), where an over-long message would
 * otherwise be a 400 that the entity-parse fallback does not catch.
 *
 * Returns the message_id of the FIRST piece, which is the one a caller would
 * quote or react to.
 */
export async function sendText(
  chatId: number,
  text: string,
  replyToMessageId?: number
): Promise<number> {
  const replyParams = replyToMessageId
    ? {
        reply_parameters: {
          message_id: replyToMessageId,
          allow_sending_without_reply: true,
        },
      }
    : {};

  const pieces = splitByLength(text);
  let firstId: number | undefined;

  for (let i = 0; i < pieces.length; i++) {
    // Only the first piece threads the reply; the rest follow it.
    const id = await sendOne(chatId, pieces[i], i === 0 ? replyParams : {});
    if (i === 0) firstId = id;
  }

  return firstId as number;
}

export async function sendReaction(
  chatId: number,
  messageId: number,
  emoji: string
): Promise<void> {
  const mappedEmoji = substituteReaction(emoji);
  try {
    // `emoji` is typed as a union of the allowed strings in grammY; our mapping
    // runs at runtime so we assert here and log any server-side REACTION_INVALID.
    const reaction: ReactionTypeEmoji = {
      type: 'emoji',
      emoji: mappedEmoji as ReactionTypeEmoji['emoji'],
    };
    await bot.api.setMessageReaction(chatId, messageId, [reaction]);
  } catch (error) {
    // A disallowed emoji becomes 400 BAD_REQUEST: REACTION_INVALID. Swallow
    // only that case — auth/network/kicked-from-chat errors must still surface
    // so they can be investigated by the bot.catch boundary.
    if (
      error instanceof GrammyError &&
      error.error_code === 400 &&
      /REACTION_INVALID/.test(error.description)
    ) {
      logger.warn(
        { err: error, chatId, messageId, emoji, mappedEmoji },
        'Reaction rejected as invalid — swallowing'
      );
      return;
    }
    throw error;
  }
}

export async function sendChatAction(
  chatId: number,
  action:
    | 'typing'
    | 'upload_photo'
    | 'record_video'
    | 'upload_video'
    | 'record_voice'
    | 'upload_voice'
    | 'upload_document'
    | 'find_location'
    | 'record_video_note'
    | 'upload_video_note'
): Promise<void> {
  await bot.api.sendChatAction(chatId, action);
}

export async function sendImage(
  chatId: number,
  buffer: Buffer,
  filename: string,
  caption?: string
): Promise<number> {
  const message = await bot.api.sendPhoto(chatId, new InputFile(buffer, filename), {
    ...(caption && { caption }),
  });
  return message.message_id;
}

export async function sendDocument(
  chatId: number,
  buffer: Buffer,
  filename: string,
  caption?: string
): Promise<number> {
  const message = await bot.api.sendDocument(chatId, new InputFile(buffer, filename), {
    ...(caption && { caption }),
  });
  return message.message_id;
}

export async function sendVoice(
  chatId: number,
  oggBuffer: Buffer,
  filename = 'reply.ogg'
): Promise<number> {
  const message = await bot.api.sendVoice(chatId, new InputFile(oggBuffer, filename));
  return message.message_id;
}

/**
 * Download a file by its Telegram file_id.
 *
 * Two-step process:
 *   1. `getFile(file_id)` returns metadata including `file_path`
 *   2. Download from `https://api.telegram.org/file/bot<TOKEN>/<file_path>`
 *
 * The cloud Bot API caps downloads at 20 MB; larger files fail at getFile
 * with "file is too big" (400). Callers convert that into a user-friendly
 * error message.
 */
export async function downloadFile(fileId: string): Promise<{ buffer: Buffer; filePath: string }> {
  const file = await bot.api.getFile(fileId);
  const filePath = file.file_path;
  if (!filePath) {
    throw new Error(`getFile returned no file_path for ${fileId}`);
  }
  const url = `https://api.telegram.org/file/bot${config.telegram.botToken}/${filePath}`;
  const response = await fetchWithTimeout(url, { method: 'GET' }, config.timeouts.default);
  if (!response.ok) {
    throw new Error(`Telegram file download failed: ${response.status} ${response.statusText}`);
  }
  const arrayBuffer = await response.arrayBuffer();
  return { buffer: Buffer.from(arrayBuffer), filePath };
}

/**
 * Send a location.
 *
 * Telegram splits what WhatsApp treats as one call: `sendLocation` takes ONLY
 * coordinates (it has no name/address parameters at all), while `sendVenue`
 * renders a titled card and *requires* both `title` and `address`. So dispatch
 * on what's actually present rather than dropping the labels.
 */
export async function sendLocation(
  chatId: number,
  latitude: number,
  longitude: number,
  name?: string,
  address?: string
): Promise<number> {
  if (name && address) {
    const venue = await bot.api.sendVenue(chatId, latitude, longitude, name, address);
    return venue.message_id;
  }
  const message = await bot.api.sendLocation(chatId, latitude, longitude);
  return message.message_id;
}

export interface ContactDetails {
  name: string;
  phone: string;
  email?: string;
  org?: string;
}

/**
 * Send a contact card.
 *
 * `sendContact` accepts only phone/first/last/vcard, so email and organization
 * are carried in a vCard — built with the same helper the WhatsApp clients use,
 * so the three clients produce identical cards.
 */
export async function sendContact(chatId: number, contact: ContactDetails): Promise<number> {
  const [firstName, ...rest] = contact.name.trim().split(/\s+/);
  const lastName = rest.join(' ');
  const needsVcard = Boolean(contact.email || contact.org);

  const message = await bot.api.sendContact(chatId, contact.phone, firstName || contact.name, {
    ...(lastName && { last_name: lastName }),
    ...(needsVcard && {
      vcard: buildVCard({
        name: contact.name,
        phone: contact.phone,
        email: contact.email,
        organization: contact.org,
      }),
    }),
  });
  return message.message_id;
}

// Exported for unit tests only.
export const _internals = { substituteReaction, REACTION_MAP };
