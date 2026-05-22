import type { TelegramContext } from '../bot.js';
import { logger } from '../logger.js';
import * as telegramApi from '../services/telegram-api.js';
import { isFileTooBigError } from './util.js';

export type PhotoExtraction =
  | { kind: 'ok'; data: string; mimetype: string }
  | { kind: 'too-large' }
  | { kind: 'download-error' };

/**
 * Download the highest-resolution size of an inbound photo and return it as
 * base64 for the AI API's vision pipeline.
 *
 * Telegram delivers photos as an array of `PhotoSize` (thumb → small → … →
 * large). The last entry is always the highest resolution.
 *
 * Bot API caps downloads at 20 MB; oversize originals surface as a
 * GrammyError at getFile time. We map that to the `too-large` kind so the
 * caller can render a distinct message instead of bucketing every failure
 * (auth, rate-limit, network) into one generic error.
 */
export async function extractPhotoData(ctx: TelegramContext): Promise<PhotoExtraction> {
  const photo = ctx.msg?.photo;
  if (!photo || photo.length === 0) return { kind: 'download-error' };

  const largest = photo[photo.length - 1];

  try {
    const { buffer } = await telegramApi.downloadFile(largest.file_id);

    // Telegram always re-encodes incoming photos as JPEG.
    const mimetype = 'image/jpeg';

    logger.info(
      {
        fileId: largest.file_id,
        size: buffer.length,
        width: largest.width,
        height: largest.height,
      },
      'Photo downloaded'
    );

    return { kind: 'ok', data: buffer.toString('base64'), mimetype };
  } catch (error) {
    if (isFileTooBigError(error)) {
      logger.warn({ fileId: largest.file_id }, 'Photo exceeds Telegram 20 MB limit');
      return { kind: 'too-large' };
    }
    logger.error({ error }, 'Error extracting photo');
    return { kind: 'download-error' };
  }
}
