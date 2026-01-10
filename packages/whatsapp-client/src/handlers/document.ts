import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { downloadMediaMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

export interface DocumentData {
  buffer: Buffer;
  mimetype: string;
  filename: string;
  caption: string | null;
}

/**
 * Download and extract document data from a WhatsApp message
 * Returns document buffer, mimetype, filename, and caption (if any)
 */
export async function extractDocumentData(
  sock: WASocket,
  msg: WAMessage
): Promise<DocumentData | null> {
  const documentMessage = msg.message?.documentMessage;
  if (!documentMessage) return null;

  try {
    // Download document from WhatsApp
    const buffer = await downloadMediaMessage(
      msg,
      'buffer',
      {},
      {
        logger: logger.child({ module: 'baileys-download' }),
        reuploadRequest: sock.updateMediaMessage,
      }
    );

    if (!buffer) {
      throw new Error('Failed to download document');
    }

    const mimetype = documentMessage.mimetype || 'application/pdf';
    const filename = documentMessage.fileName || `document_${Date.now()}.pdf`;
    const caption = documentMessage.caption || null;

    logger.info(
      { size: buffer.length, mimetype, filename, hasCaption: !!caption },
      'Document downloaded'
    );

    return {
      buffer: buffer as Buffer,
      mimetype,
      filename,
      caption,
    };
  } catch (error) {
    logger.error({ error }, 'Error downloading document');
    return null;
  }
}
