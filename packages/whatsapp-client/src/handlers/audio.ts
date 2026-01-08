import type { WASocket, WAMessage } from '@whiskeysockets/baileys';
import { downloadMediaMessage } from '@whiskeysockets/baileys';
import { config } from '../config.js';
import { logger } from '../logger.js';

/**
 * Transcribe audio message to text
 * Returns transcription string or null on failure
 */
export async function transcribeAudioMessage(
  sock: WASocket,
  msg: WAMessage
): Promise<string | null> {
  const audioMessage = msg.message?.audioMessage;
  if (!audioMessage) return null;

  try {
    // Download audio from WhatsApp
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
      throw new Error('Failed to download audio');
    }

    const mimetype = audioMessage.mimetype || 'audio/ogg';
    const extension = mimetype.split('/')[1]?.split(';')[0] || 'ogg';
    const filename = `audio_${Date.now()}.${extension}`;

    logger.info({ filename, size: buffer.length, mimetype }, 'Audio downloaded');

    // Transcribe via API
    const transcription = await transcribeAudio(buffer, mimetype, filename);
    logger.info({ transcription }, 'Audio transcribed');

    return transcription;
  } catch (error) {
    logger.error({ error }, 'Error transcribing audio');
    return null;
  }
}

/**
 * Call transcription API
 */
async function transcribeAudio(
  buffer: Buffer,
  mimetype: string,
  filename: string
): Promise<string> {
  const blob = new Blob([buffer], { type: mimetype });
  const formData = new FormData();
  formData.append('file', blob, filename);

  const response = await fetch(`${config.aiApiUrl}/transcribe`, {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const contentType = response.headers.get('content-type');
    if (contentType?.includes('application/json')) {
      const errorData = await response.json();
      const errorMsg = errorData.detail || 'Transcription failed';

      if (response.status === 503) {
        logger.error({ status: 503, detail: errorMsg }, 'STT service not configured');
      }

      throw new Error(errorMsg);
    } else {
      const text = await response.text();
      logger.error({ status: response.status, body: text }, 'Unexpected transcription error');
      throw new Error(`Transcription failed with status ${response.status}`);
    }
  }

  const { transcription } = await response.json();
  return transcription;
}
