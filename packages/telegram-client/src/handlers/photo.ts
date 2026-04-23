import type { TelegramContext } from '../bot.js';
import { logger } from '../logger.js';
import * as telegramApi from '../services/telegram-api.js';

/**
 * Download the highest-resolution size of an inbound photo and return it as
 * base64 for the AI API's vision pipeline.
 *
 * Telegram delivers photos as an array of `PhotoSize` (thumb → small → … →
 * large). The last entry is always the highest resolution.
 */
export async function extractPhotoData(
  ctx: TelegramContext
): Promise<{ data: string; mimetype: string } | null> {
  const photo = ctx.msg?.photo;
  if (!photo || photo.length === 0) return null;

  try {
    const largest = photo[photo.length - 1];
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

    return { data: buffer.toString('base64'), mimetype };
  } catch (error) {
    logger.error({ error }, 'Error extracting photo');
    return null;
  }
}
