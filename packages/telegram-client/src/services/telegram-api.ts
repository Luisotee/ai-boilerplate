/**
 * Telegram Bot API wrapper used for outbound calls that originate OUTSIDE the
 * grammY update loop — e.g. the AI API calling POST /telegram/send-text to
 * deliver a response. Inside handlers, prefer ctx.reply / ctx.replyWithVoice /
 * ctx.react directly.
 */

import { InputFile } from 'grammy';
import type { ReactionTypeEmoji } from 'grammy/types';
import { bot } from '../bot.js';
import { config } from '../config.js';
import { logger } from '../logger.js';
import { fetchWithTimeout } from '../utils/fetch.js';

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

export async function sendText(
  chatId: number,
  text: string,
  replyToMessageId?: number
): Promise<number> {
  const message = await bot.api.sendMessage(chatId, text, {
    ...(replyToMessageId && {
      reply_parameters: { message_id: replyToMessageId, allow_sending_without_reply: true },
    }),
  });
  return message.message_id;
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
    // A disallowed emoji becomes 400 BAD_REQUEST: REACTION_INVALID. Log and
    // swallow — reactions are nice-to-have, never critical.
    logger.warn({ error, chatId, messageId, emoji, mappedEmoji }, 'Reaction rejected by Telegram');
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

// Exported for unit tests only.
export const _internals = { substituteReaction, REACTION_MAP };
