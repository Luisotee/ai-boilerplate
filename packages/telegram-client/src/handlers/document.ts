import { GrammyError } from 'grammy';
import type { TelegramContext } from '../bot.js';
import { logger } from '../logger.js';
import * as telegramApi from '../services/telegram-api.js';

export type DocExtraction =
  | { kind: 'ok'; data: string; mimetype: string; filename: string }
  | { kind: 'wrong-type'; mimetype: string }
  | { kind: 'too-large' }
  | { kind: 'download-error' };

/**
 * Download a document and return it as base64. Only PDFs are accepted for AI
 * processing.
 *
 * Bot API caps downloads at 20 MB; oversize files surface as a GrammyError at
 * getFile time with description containing "file is too big" — we map that to
 * the `too-large` kind so the caller can show a distinct user message.
 */
export async function extractDocumentData(ctx: TelegramContext): Promise<DocExtraction> {
  const doc = ctx.msg?.document;
  if (!doc) return { kind: 'download-error' };

  const mimetype = doc.mime_type ?? 'application/octet-stream';
  const filename = doc.file_name ?? 'document.pdf';

  if (mimetype !== 'application/pdf') {
    logger.warn({ mimetype, filename }, 'Non-PDF document rejected');
    return { kind: 'wrong-type', mimetype };
  }

  try {
    const { buffer } = await telegramApi.downloadFile(doc.file_id);

    logger.info(
      { fileId: doc.file_id, mimetype, filename, size: buffer.length },
      'Document downloaded'
    );

    return { kind: 'ok', data: buffer.toString('base64'), mimetype, filename };
  } catch (error) {
    if (isFileTooBigError(error)) {
      logger.warn({ fileId: doc.file_id, filename }, 'Document exceeds Telegram 20 MB limit');
      return { kind: 'too-large' };
    }
    logger.error({ error }, 'Error extracting document');
    return { kind: 'download-error' };
  }
}

function isFileTooBigError(error: unknown): boolean {
  return (
    error instanceof GrammyError &&
    error.error_code === 400 &&
    /file is too big/i.test(error.description)
  );
}
