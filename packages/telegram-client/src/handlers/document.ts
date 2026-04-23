import type { TelegramContext } from '../bot.js';
import { logger } from '../logger.js';
import * as telegramApi from '../services/telegram-api.js';

/**
 * Download a document and return it as base64. Only PDFs are accepted for AI
 * processing — other types are rejected (returns null).
 *
 * Bot API caps downloads at 20 MB; larger files return 400 at getFile time
 * and surface as a download error here, which we convert to null.
 */
export async function extractDocumentData(
  ctx: TelegramContext
): Promise<{ data: string; mimetype: string; filename: string } | null> {
  const doc = ctx.msg?.document;
  if (!doc) return null;

  const mimetype = doc.mime_type ?? 'application/octet-stream';
  const filename = doc.file_name ?? 'document.pdf';

  if (mimetype !== 'application/pdf') {
    logger.warn({ mimetype, filename }, 'Non-PDF document rejected');
    return null;
  }

  try {
    const { buffer } = await telegramApi.downloadFile(doc.file_id);

    logger.info(
      { fileId: doc.file_id, mimetype, filename, size: buffer.length },
      'Document downloaded'
    );

    return { data: buffer.toString('base64'), mimetype, filename };
  } catch (error) {
    logger.error({ error }, 'Error extracting document');
    return null;
  }
}
