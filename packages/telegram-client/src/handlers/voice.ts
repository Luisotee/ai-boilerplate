import type { TelegramContext } from '../bot.js';
import { transcribeAudio } from '../api-client.js';
import { logger } from '../logger.js';
import * as telegramApi from '../services/telegram-api.js';
import { isFileTooBigError } from './util.js';

export type VoiceExtraction =
  | { kind: 'ok'; transcription: string }
  | { kind: 'too-large' }
  | { kind: 'transcription-failed' }
  | { kind: 'download-error' }
  | { kind: 'no-voice' };

/**
 * Download a voice note (or fallback audio message) and transcribe it via the
 * AI API. Returns a discriminated result so callers can show distinct
 * user-facing replies for download vs transcription failures vs oversize-file
 * errors (Bot API 20 MB download limit).
 *
 * Telegram voice notes are always OGG/Opus (mime_type "audio/ogg") which the
 * AI API /transcribe endpoint accepts directly.
 */
export async function extractAndTranscribeVoice(ctx: TelegramContext): Promise<VoiceExtraction> {
  const voice = ctx.msg?.voice ?? ctx.msg?.audio;
  if (!voice) return { kind: 'no-voice' };

  let buffer: Buffer;
  let filePath: string;
  try {
    ({ buffer, filePath } = await telegramApi.downloadFile(voice.file_id));
  } catch (error) {
    if (isFileTooBigError(error)) {
      logger.warn({ fileId: voice.file_id }, 'Voice exceeds Telegram 20 MB limit');
      return { kind: 'too-large' };
    }
    logger.error({ error }, 'Error downloading voice message');
    return { kind: 'download-error' };
  }

  const mimetype = voice.mime_type ?? 'audio/ogg';
  const extension = filePath.split('.').pop() ?? 'ogg';
  const filename = `voice_${Date.now()}.${extension}`;

  logger.info({ fileId: voice.file_id, size: buffer.length, mimetype }, 'Voice downloaded');

  try {
    const transcription = await transcribeAudio(buffer, mimetype, filename);
    if (!transcription) return { kind: 'transcription-failed' };
    return { kind: 'ok', transcription };
  } catch (error) {
    logger.error({ error }, 'Error transcribing voice message');
    return { kind: 'transcription-failed' };
  }
}
