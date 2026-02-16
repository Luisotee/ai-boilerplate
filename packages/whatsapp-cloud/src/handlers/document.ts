import * as graphApi from '../services/graph-api.js';
import { logger } from '../logger.js';

/**
 * Download a document from the Graph API and return it as base64-encoded data.
 * Only PDF documents are accepted for AI processing; other types are rejected.
 *
 * @param mediaId - The Cloud API media ID for the document
 * @param mimetype - The document MIME type
 * @param filename - The document filename
 * @returns Base64 data, mimetype, and filename, or null on failure/rejection
 */
export async function extractDocumentData(
  mediaId: string,
  mimetype: string,
  filename: string
): Promise<{ data: string; mimetype: string; filename: string } | null> {
  try {
    // Only accept PDFs for AI processing
    if (mimetype !== 'application/pdf') {
      logger.warn({ mimetype, filename }, 'Non-PDF document rejected');
      return null;
    }

    const { buffer } = await graphApi.downloadMedia(mediaId);

    logger.info(
      { mediaId, mimetype, filename, size: buffer.length },
      'Document downloaded from Graph API'
    );

    return {
      data: buffer.toString('base64'),
      mimetype,
      filename,
    };
  } catch (error) {
    logger.error({ error, mediaId }, 'Error extracting document data');
    return null;
  }
}
