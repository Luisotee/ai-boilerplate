import { InputFile } from 'grammy';
import type { TelegramContext } from '../bot.js';
import { sendMessageToAI, getUserPreferences, textToSpeech } from '../api-client.js';
import { config } from '../config.js';
import { logger } from '../logger.js';
import { sleep, splitResponseIntoBursts, stripSplitDelimiters } from '../utils/message-split.js';
import { chatIdToJid, chatTypeToConversationType } from '../utils/telegram-id.js';
import { messagesSent } from '../routes/metrics.js';
import * as telegramApi from '../services/telegram-api.js';

interface ImageData {
  data: string;
  mimetype: string;
}

interface DocumentData {
  data: string;
  mimetype: string;
  filename: string;
}

interface HandleOptions {
  senderJid?: string;
  senderName?: string;
  isGroupAdmin?: boolean;
  saveOnly?: boolean;
  image?: ImageData;
  document?: DocumentData;
}

/**
 * Funnel every inbound handler into this orchestrator. It owns:
 *  - enqueue to the AI API (or save-only for group non-mentions)
 *  - multi-burst response delivery with per-chunk delays
 *  - TTS → voice-note reply when the user has it enabled
 *  - failure reaction on unexpected errors
 */
export async function handleTextMessage(
  ctx: TelegramContext,
  text: string,
  options: HandleOptions = {}
): Promise<void> {
  const chatId = ctx.chat?.id;
  const messageId = ctx.msg?.message_id;
  if (chatId === undefined || messageId === undefined) {
    logger.warn({ update: ctx.update.update_id }, 'Missing chat or message context');
    return;
  }

  const jid = chatIdToJid(chatId);
  const chatType = ctx.chat?.type ?? 'private';
  const conversationType = chatTypeToConversationType(chatType);
  const senderName =
    options.senderName ??
    (ctx.from
      ? `${ctx.from.first_name ?? ''} ${ctx.from.last_name ?? ''}`.trim() ||
        ctx.from.username ||
        'Unknown'
      : 'Unknown');

  if (options.saveOnly) {
    logger.debug({ jid, text: text.slice(0, 50) }, 'Saving group message to history');
    try {
      await sendMessageToAI(jid, text, {
        conversationType,
        senderJid: options.senderJid,
        senderName,
        messageId: String(messageId),
        saveOnly: true,
      });
    } catch (error) {
      logger.warn({ error, jid }, 'Failed to save group message to history');
    }
    return;
  }

  // Keep the "typing…" indicator alive for the full duration of this handler.
  // @grammyjs/auto-chat-action refreshes every ~5s until we return.
  ctx.chatAction = 'typing';

  logger.info(
    {
      from: jid,
      text,
      conversationType,
      hasImage: !!options.image,
      hasDocument: !!options.document,
    },
    'Received message'
  );

  try {
    const response = await sendMessageToAI(jid, text, {
      conversationType,
      senderJid: options.senderJid,
      senderName,
      messageId: String(messageId),
      isGroupAdmin: options.isGroupAdmin,
      image: options.image,
      document: options.document,
    });

    if (!response) return;

    const chunks = splitResponseIntoBursts(response, {
      disabled: !config.messageSplit.enabled || conversationType === 'group',
      maxChunks: config.messageSplit.maxChunks,
    });

    let sentCount = 0;
    try {
      for (let i = 0; i < chunks.length; i++) {
        if (i > 0) {
          const delay = Math.min(
            config.messageSplit.maxDelayMs,
            config.messageSplit.baseDelayMs + chunks[i].length * config.messageSplit.perCharMs
          );
          await sleep(delay);
        }
        await ctx.reply(chunks[i], {
          reply_parameters:
            i === 0 ? { message_id: messageId, allow_sending_without_reply: true } : undefined,
        });
        sentCount++;
        messagesSent.inc({ client: 'telegram', type: 'text' });
      }
    } catch (burstErr) {
      if (sentCount === 0) throw burstErr;
      logger.warn(
        { error: burstErr, jid, sentCount, totalChunks: chunks.length },
        'Burst send failed mid-stream; partial response delivered'
      );
    }

    const partial = sentCount < chunks.length;
    logger[partial ? 'warn' : 'info'](
      { jid, responseLength: response.length, chunkCount: chunks.length, sentCount },
      partial ? 'Partially sent AI response' : 'Sent AI response'
    );

    // --- Text delivered; failures below must NOT trigger the failure reaction ---

    try {
      const prefs = await getUserPreferences(jid);
      if (prefs?.tts_enabled) {
        logger.info({ jid }, 'TTS enabled, generating voice message');
        const audioBuffer = await textToSpeech(stripSplitDelimiters(response), jid);
        if (audioBuffer) {
          await ctx.replyWithVoice(new InputFile(audioBuffer, 'reply.ogg'));
          messagesSent.inc({ client: 'telegram', type: 'audio' });
          logger.info({ jid }, 'Voice message sent');
        } else {
          logger.warn({ jid }, 'TTS failed, text-only sent');
        }
      }
    } catch (ttsError) {
      logger.warn({ error: ttsError, jid }, 'TTS delivery failed, text-only sent');
    }
  } catch (error) {
    logger.error({ error, jid, messageId }, 'Error handling text message');
    try {
      await telegramApi.sendReaction(chatId, messageId, '❌');
    } catch (reactionError) {
      logger.warn({ error: reactionError, jid }, 'Failed to send failure reaction');
    }
    try {
      await ctx.reply('Sorry, I encountered an error processing your message. Please try again.');
    } catch (replyError) {
      logger.warn({ error: replyError, jid }, 'Failed to send error reply');
    }
  }
}
