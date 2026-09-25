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
import { handleSharedContact, offerPhoneLink } from './handlers/link-prompt.js';
import { logger } from './logger.js';
import * as telegramApi from './services/telegram-api.js';
import { chatIdToJid, chatTypeToConversationType } from './utils/telegram-id.js';
import { isAddressedToBot, stripBotMention } from './utils/mention.js';
import { documentMarker, imageMarker } from './utils/group-media-marker.js';
import { isWhitelisted } from './utils/whitelist.js';
import { decideGroupGating } from './utils/gating.js';
import { isSenderGroupAdmin } from './services/group-admin.js';

export function registerUpdateHandlers(): void {
  // ---------------- Account linking (phone share) ----------------
  // Registered FIRST: grammY runs middleware in registration order, and the
  // catch-all `message:text` below would otherwise forward /linkphone to the
  // AI API as ordinary text. `/link` and `/link <code>` still go to the AI API,
  // which owns the code flow.
  bot.command('linkphone', async (ctx) => {
    // saveOnly: an un-addressed or (GROUP_GATING=membership) non-whitelisted
    // group sender gets no reply at all, not even the "private only" hint.
    const { skip, saveOnly } = resolveGate(ctx);
    if (skip || saveOnly) return;
    await offerPhoneLink(ctx);
  });

  bot.on('message:contact', async (ctx) => {
    if (resolveGate(ctx).skip) return;
    await handleSharedContact(ctx);
  });

  // ---------------- Text ----------------
  bot.on('message:text', async (ctx) => {
    const { skip, isGroup, saveOnly } = resolveGate(ctx);
    if (skip) return;

    const text = ctx.msg.text;
    // Only strip the mention when answering; a saved-only transcript line
    // should read exactly as it was written in the group.
    const cleanText = isGroup && !saveOnly ? stripBotMentionFromCtx(ctx, text) : text;

    // The AI API gates group admin commands and agent tools that change a
    // group's settings (e.g. "@bot stop the announcements here") and fails
    // closed: anything but an explicit `true` is refused. So resolve admin
    // status for every ADDRESSED group message — the one that is about to cost
    // an AI call anyway. Un-addressed chatter (saved only) never pays for a
    // getChatMember round trip.
    const isGroupAdmin = isGroup && !saveOnly ? await isSenderGroupAdmin(ctx) : undefined;

    await handleTextMessage(ctx, cleanText, {
      senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
      saveOnly,
      isGroupAdmin,
    });
  });

  // ---------------- Voice / Audio ----------------
  bot.on(['message:voice', 'message:audio'], async (ctx) => {
    const { skip, saveOnly } = resolveGate(ctx);
    if (skip) return;

    if (saveOnly) {
      // For voice we don't save a transcript on non-mention — matches Baileys
      // behavior (no attempt to transcribe group chatter just for context).
      return;
    }

    ctx.chatAction = 'typing';
    const result = await extractAndTranscribeVoice(ctx);

    if (result.kind === 'ok') {
      await handleTextMessage(ctx, result.transcription, {
        senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
      });
      return;
    }

    if (result.kind === 'no-voice') {
      // Defensive — the filter query above shouldn't deliver these, but bail
      // silently if it does.
      return;
    }

    logger.warn(
      { updateId: ctx.update.update_id, kind: result.kind },
      'Voice handler did not produce a transcription'
    );

    const chatId = ctx.chat?.id;
    const messageId = ctx.msg?.message_id;
    if (chatId !== undefined && messageId !== undefined) {
      try {
        await telegramApi.sendReaction(chatId, messageId, '❌');
      } catch (reactionError) {
        logger.warn({ error: reactionError }, 'Failed to send failure reaction');
      }
    }

    switch (result.kind) {
      case 'too-large':
        await ctx.reply(
          "Sorry, that voice message is larger than Telegram's 20 MB bot download limit. Please send a shorter clip."
        );
        break;
      case 'download-error':
        await ctx.reply("Sorry, I couldn't download that voice message. Please try again.");
        break;
      case 'transcription-failed':
        await ctx.reply("Sorry, I couldn't transcribe that voice message. Please try again.");
        break;
    }
  });

  // ---------------- Photo ----------------
  bot.on('message:photo', async (ctx) => {
    const { skip, saveOnly } = resolveGate(ctx);
    if (skip) return;

    const caption = ctx.msg.caption ?? '';

    // Group non-mention: save an [Image] / [Image: caption] marker to history
    // without downloading the binary. Matches Baileys (whatsapp.ts:226-234) so
    // the AI keeps image context across both clients.
    if (saveOnly) {
      await handleTextMessage(ctx, imageMarker(caption), {
        senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
        saveOnly: true,
      });
      return;
    }

    ctx.chatAction = 'typing';
    const result = await extractPhotoData(ctx);
    const text = caption || 'Image received';

    switch (result.kind) {
      case 'ok':
        await handleTextMessage(ctx, text, {
          senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
          image: { data: result.data, mimetype: result.mimetype },
        });
        break;
      case 'too-large':
        await ctx.reply(
          "Sorry, that image is larger than Telegram's 20 MB bot download limit. Please send a smaller file."
        );
        break;
      case 'download-error':
        await ctx.reply("Sorry, I couldn't download that image. Please try again.");
        break;
    }
  });

  // ---------------- Document ----------------
  bot.on('message:document', async (ctx) => {
    const { skip, saveOnly } = resolveGate(ctx);
    if (skip) return;

    const caption = ctx.msg.caption ?? '';
    const filename = ctx.msg.document?.file_name ?? 'document.pdf';

    // Group non-mention: save a [Document: filename] marker without
    // downloading the file. Matches Baileys (whatsapp.ts:262-273).
    if (saveOnly) {
      await handleTextMessage(ctx, documentMarker(filename, caption), {
        senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
        saveOnly: true,
      });
      return;
    }

    ctx.chatAction = 'typing';
    const result = await extractDocumentData(ctx);
    const text = caption || `Document: ${filename}`;

    switch (result.kind) {
      case 'ok':
        await handleTextMessage(ctx, text, {
          senderJid: ctx.from ? chatIdToJid(ctx.from.id) : undefined,
          document: { data: result.data, mimetype: result.mimetype, filename: result.filename },
        });
        break;
      case 'wrong-type':
        await ctx.reply('Sorry, I can only process PDF documents. Please send a PDF file.');
        break;
      case 'too-large':
        await ctx.reply(
          "Sorry, that PDF is larger than Telegram's 20 MB bot download limit. Please send a smaller file."
        );
        break;
      case 'download-error':
        await ctx.reply("Sorry, I couldn't download that file. Please try again.");
        break;
    }
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Telegram matches on the `tg:<chat_id>` id only — the Bot API exposes no phone
 * number for a chat, so no phone is passed. Phone-shaped entries in a mixed
 * whitelist live in a separate set the matcher never consults here, so a bare
 * number can never accidentally admit a Telegram chat with the same digits.
 */
function passesWhitelist(chatId: number | undefined): boolean {
  if (config.whitelistPhones.size === 0) return true;
  if (chatId === undefined) return false;
  return isWhitelisted(config.whitelistPhones, chatIdToJid(chatId));
}

interface GateResult {
  /** Drop entirely — not saved, not answered. */
  skip: boolean;
  isGroup: boolean;
  /** Save to history without generating a response. */
  saveOnly: boolean;
}

/**
 * The whitelist gate + save-only decision for one update (utils/gating.ts, the
 * truth table shared with Baileys).
 *
 * `jid` mode (default) is the historical behaviour: the CHAT id must be listed
 * — a private chat's id is its user's id, a group's is the group's — and then
 * every member of a listed group may address the bot.
 *
 * `membership` mode: the Bot API cannot enumerate a group's members, so there
 * is no "has a whitelisted member" check to make — every group the bot is in
 * is in scope and its transcript is saved, but only a whitelisted SENDER
 * (`tg:<from.id>`) gets a reply, unless the group itself is listed. This
 * client's decision is the only gate there is for Telegram groups in that
 * mode: the AI API admits every group JID then.
 */
function resolveGate(ctx: TelegramContext): GateResult {
  const conversationType = chatTypeToConversationType(ctx.chat?.type ?? 'private');
  const isGroup = conversationType === 'group';
  const whitelistEnabled = config.whitelistPhones.size > 0;
  const chatListed = passesWhitelist(ctx.chat?.id);
  const membership = isGroup && config.groupGating === 'membership';
  const senderWhitelisted = membership
    ? ctx.from !== undefined && isWhitelisted(config.whitelistPhones, chatIdToJid(ctx.from.id))
    : chatListed;

  const decision = decideGroupGating({
    isGroup,
    whitelistEnabled,
    senderWhitelisted,
    groupExplicit: chatListed,
    groupAllowed: membership ? true : chatListed,
    respondInGroup: isGroup && isAddressed(ctx),
  });
  if (decision.skip) {
    logger.debug(
      { chatId: ctx.chat?.id, isGroup, groupGating: config.groupGating },
      'Skipping non-whitelisted chat'
    );
  }
  return { skip: decision.skip, isGroup, saveOnly: decision.saveOnly };
}

// Exported for unit tests only.
export const _internals = { passesWhitelist, resolveGate };

/**
 * `bot.botInfo` THROWS when the bot has not been initialized (grammY >= 1.4x) —
 * it is not merely undefined — so every read must be guarded by `isInited()`.
 * main.ts awaits `bot.init()` before accepting webhook deliveries, so this
 * should never be false in production; the guard exists so a misordered
 * bootstrap degrades to "does not answer in groups" instead of throwing on
 * every single update.
 */
function botIdentity(): { id: number; username: string } | undefined {
  if (!bot.isInited()) {
    logger.error(
      'bot.botInfo unavailable — bot.init() has not completed. Group @-mentions cannot be detected.'
    );
    return undefined;
  }
  return { id: bot.botInfo.id, username: bot.botInfo.username };
}

function isAddressed(ctx: TelegramContext): boolean {
  const message = ctx.msg as Message | undefined;
  const me = botIdentity();
  if (!message || !me) return false;
  return isAddressedToBot(message, me);
}

function stripBotMentionFromCtx(ctx: TelegramContext, text: string): string {
  const me = botIdentity();
  if (!me) return text;
  return stripBotMention(text, me);
}
