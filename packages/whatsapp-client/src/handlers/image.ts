import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { downloadMediaMessage } from '@whiskeysockets/baileys';
import { logger } from '../logger.js';

export interface ImageData {
  buffer: Buffer;
  mimetype: string;
  caption: string | null;
}

/**
 * Download and extract image data from a WhatsApp message
 * Returns image buffer, mimetype, and caption (if any)
 */
export async function extractImageData(sock: WASocket, msg: WAMessage): Promise<ImageData | null> {
  const imageMessage = msg.message?.imageMessage;
  if (!imageMessage) return null;

  try {
    // Download image from WhatsApp
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
      throw new Error('Failed to download image');
    }

    const mimetype = imageMessage.mimetype || 'image/jpeg';
    const caption = imageMessage.caption || null;

    logger.info({ size: buffer.length, mimetype, hasCaption: !!caption }, 'Image downloaded');

    return {
      buffer: buffer as Buffer,
      mimetype,
      caption,
    };
  } catch (error) {
    logger.error({ error }, 'Error downloading image');
    return null;
  }
}
