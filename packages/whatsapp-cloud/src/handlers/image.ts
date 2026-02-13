import * as graphApi from '../services/graph-api.js';
import { logger } from '../logger.js';

/**
 * Download an image from the Graph API and return it as base64-encoded data.
 *
 * @param mediaId - The Cloud API media ID for the image
 * @returns Base64 data and mimetype, or null on failure
 */
export async function extractImageData(
  mediaId: string
): Promise<{ data: string; mimetype: string } | null> {
  try {
    const { buffer, mimetype } = await graphApi.downloadMedia(mediaId);

    logger.info({ mediaId, mimetype, size: buffer.length }, 'Image downloaded from Graph API');

    return {
      data: buffer.toString('base64'),
      mimetype,
    };
  } catch (error) {
    logger.error({ error, mediaId }, 'Error extracting image data');
    return null;
  }
}
