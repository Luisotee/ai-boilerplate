import type { TelegramContext } from '../bot.js';
import { transcribeAudio } from '../api-client.js';
import { logger } from '../logger.js';
import * as telegramApi from '../services/telegram-api.js';

/**
 * Download a voice note (or fallback audio message) and transcribe it via the
 * AI API. Returns the transcription text or null on failure.
 *
 * Telegram voice notes are always OGG/Opus (mime_type "audio/ogg") which the
 * AI API /transcribe endpoint accepts directly.
 */
export async function extractAndTranscribeVoice(ctx: TelegramContext): Promise<string | null> {
  const voice = ctx.msg?.voice ?? ctx.msg?.audio;
  if (!voice) return null;

  try {
    const { buffer, filePath } = await telegramApi.downloadFile(voice.file_id);
    const mimetype = voice.mime_type ?? 'audio/ogg';
    const extension = filePath.split('.').pop() ?? 'ogg';
    const filename = `voice_${Date.now()}.${extension}`;

    logger.info({ fileId: voice.file_id, size: buffer.length, mimetype }, 'Voice downloaded');

    return await transcribeAudio(buffer, mimetype, filename);
  } catch (error) {
    logger.error({ error }, 'Error transcribing voice message');
    return null;
  }
}
