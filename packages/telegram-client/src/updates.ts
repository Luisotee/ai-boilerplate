/**
 * grammY update dispatch — wires bot.on(...) handlers to our internal
 * handlers. Called once from main.ts, before the Fastify server accepts
 * webhook deliveries.
 */
import type { Message } from 'grammy/types';
import { bot, type TelegramContext } from './bot.js';
import { config } from './config.js';
import { handleTextMessage } from './handlers/text.js';
import { extractAndTranscribeVoice } from './handlers/voice.js';
import { extractPhotoData } from './handlers/photo.js';
import { extractDocumentData } from './handlers/document.js';
import { logger } from './logger.js';
import { messagesReceived } from './routes/metrics.js';
import { chatIdToJid, chatTypeToConversationType } from './utils/telegram-id.js';
import { isAddressedToBot, stripBotMention } from './utils/mention.js';

export function registerUpdateHandlers(): void {
  // ---------------- Text ----------------
  bot.on('message:text', async (ctx) => {
    const chatType = ctx.chat?.type ?? 'private';
    const conversationType = chatTypeToConversationType(chatType);
    if (passesWhitelist(ctx.chat?.id) === false) return;
    messagesReceived.inc({ type: 'text', conversation_type: conversationType });

    const text = ctx.msg.text;
    const isGroup = conversationType === 'group';
    const addressed = !isGroup || isAddressed(ctx);
    const cleanText = addressed && isGroup ? stripBotMentionFromCtx(ctx, text) : text;

    await handleTextMessage(ctx, cleanText, {
      senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
      saveOnly: isGroup && !addressed,
    });
  });

  // ---------------- Voice / Audio ----------------
  bot.on(['message:voice', 'message:audio'], async (ctx) => {
    const chatType = ctx.chat?.type ?? 'private';
    const conversationType = chatTypeToConversationType(chatType);
    if (passesWhitelist(ctx.chat?.id) === false) return;
    messagesReceived.inc({ type: 'voice', conversation_type: conversationType });

    const isGroup = conversationType === 'group';
    const addressed = !isGroup || isAddressed(ctx);
    if (isGroup && !addressed) {
      // For voice we don't save a transcript on non-mention — matches Baileys
      // behavior (no attempt to transcribe group chatter just for context).
      return;
    }

    ctx.chatAction = 'typing';
    const transcription = await extractAndTranscribeVoice(ctx);
    if (!transcription) {
      logger.warn({ updateId: ctx.update.update_id }, 'Voice transcription failed, skipping');
      return;
    }
    await handleTextMessage(ctx, transcription, {
      senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
    });
  });

  // ---------------- Photo ----------------
  bot.on('message:photo', async (ctx) => {
    const chatType = ctx.chat?.type ?? 'private';
    const conversationType = chatTypeToConversationType(chatType);
    if (passesWhitelist(ctx.chat?.id) === false) return;
    messagesReceived.inc({ type: 'photo', conversation_type: conversationType });

    const isGroup = conversationType === 'group';
    const addressed = !isGroup || isAddressed(ctx);
    if (isGroup && !addressed) return;

    ctx.chatAction = 'typing';
    const photo = await extractPhotoData(ctx);
    const caption = ctx.msg.caption ?? '';
    const text = caption || 'Image received';

    if (photo) {
      await handleTextMessage(ctx, text, {
        senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
        image: photo,
      });
    } else {
      await ctx.reply('Sorry, I could not process that image. Please try again.');
    }
  });

  // ---------------- Document ----------------
  bot.on('message:document', async (ctx) => {
    const chatType = ctx.chat?.type ?? 'private';
    const conversationType = chatTypeToConversationType(chatType);
    if (passesWhitelist(ctx.chat?.id) === false) return;
    messagesReceived.inc({ type: 'document', conversation_type: conversationType });

    const isGroup = conversationType === 'group';
    const addressed = !isGroup || isAddressed(ctx);
    if (isGroup && !addressed) return;

    ctx.chatAction = 'typing';
    const doc = await extractDocumentData(ctx);
    const caption = ctx.msg.caption ?? '';
    const filename = ctx.msg.document?.file_name ?? 'document.pdf';
    const text = caption || `Document: ${filename}`;

    if (doc) {
      await handleTextMessage(ctx, text, {
        senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
        document: doc,
      });
    } else {
      await ctx.reply('Sorry, I can only process PDF documents. Please send a PDF file.');
    }
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function passesWhitelist(chatId: number | undefined): boolean {
  if (config.whitelistPhones.size === 0) return true;
  if (chatId === undefined) return false;
  return config.whitelistPhones.has(chatIdToJid(chatId));
}

function isAddressed(ctx: TelegramContext): boolean {
  const message = ctx.msg as Message | undefined;
  const me = bot.botInfo;
  if (!message || !me) return false;
  return isAddressedToBot(message, { id: me.id, username: me.username });
}

function stripBotMentionFromCtx(ctx: TelegramContext, text: string): string {
  const me = bot.botInfo;
  if (!me) return text;
  return stripBotMention(text, { id: me.id, username: me.username });
}
