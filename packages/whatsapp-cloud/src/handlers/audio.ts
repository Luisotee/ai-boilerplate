import * as graphApi from '../services/graph-api.js';
import { fetchWithTimeout } from '../utils/fetch.js';
import { config } from '../config.js';
import { logger } from '../logger.js';

/**
 * Download audio from the Graph API and transcribe it via the AI API.
 *
 * @param mediaId - The Cloud API media ID for the audio
 * @param mimetype - The audio MIME type (e.g., "audio/ogg")
 * @returns The transcription text, or null on failure
 */
export async function extractAndTranscribeAudio(
  mediaId: string,
  mimetype: string
): Promise<string | null> {
  try {
    // Step 1: Download audio from Graph API
    const { buffer } = await graphApi.downloadMedia(mediaId);

    const extension = mimetype.split('/')[1]?.split(';')[0] || 'ogg';
    const filename = `audio_${Date.now()}.${extension}`;

    logger.info({ filename, size: buffer.length, mimetype }, 'Audio downloaded from Graph API');

    // Step 2: Call AI API /transcribe
    const formData = new FormData();
    formData.append('file', new Blob([new Uint8Array(buffer)], { type: mimetype }), filename);

    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/transcribe`,
      {
        method: 'POST',
        headers: { 'X-API-Key': config.aiApiKey },
        body: formData,
      },
      config.timeouts.transcription
    );

    if (!response.ok) {
      if (response.status === 503) {
        logger.warn('Transcription service unavailable');
        return null;
      }

      const contentType = response.headers.get('content-type');
      if (contentType?.includes('application/json')) {
        const errorData = await response.json();
        const errorMsg = errorData.detail || 'Transcription failed';
        throw new Error(errorMsg);
      } else {
        const text = await response.text();
        logger.error({ status: response.status, body: text }, 'Unexpected transcription error');
        throw new Error(`Transcription failed with status ${response.status}`);
      }
    }

    const result = await response.json();
    const transcription = result.transcription || null;

    logger.info({ transcription }, 'Audio transcribed');
    return transcription;
  } catch (error) {
    logger.error({ error, mediaId }, 'Error transcribing audio');
    return null;
  }
}
