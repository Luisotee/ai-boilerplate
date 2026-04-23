import { config } from './config.js';
import { logger } from './logger.js';
import { apiPollDuration } from './routes/metrics.js';
import { fetchWithTimeout } from './utils/fetch.js';

function aiApiHeaders(contentType?: string): Record<string, string> {
  const headers: Record<string, string> = { 'X-API-Key': config.aiApiKey };
  if (contentType) headers['Content-Type'] = contentType;
  return headers;
}

interface ImagePayload {
  data: string; // base64 encoded
  mimetype: string;
}

interface DocumentPayload {
  data: string; // base64 encoded
  mimetype: string;
  filename: string;
}

interface MessageOptions {
  conversationType: 'private' | 'group';
  senderJid?: string;
  senderName?: string;
  saveOnly?: boolean;
  messageId?: string;
  image?: ImagePayload;
  document?: DocumentPayload;
  isGroupAdmin?: boolean;
}

interface JobStatus {
  job_id: string;
  status: string;
  complete: boolean;
  full_response?: string;
}

interface EnqueueResponse {
  job_id?: string;
  status?: string;
  message?: string;
  is_command?: boolean;
  response?: string;
}

interface UserPreferences {
  tts_enabled: boolean;
  tts_language: string;
  stt_language: string | null;
}

/**
 * Forward a message to the AI API — either save-only (group non-mentions)
 * or enqueue-and-poll for a response.
 *
 * `jid` is the synthetic "tg:<chat_id>" string; the AI API stores it in the
 * `whatsapp_jid` column as an opaque identifier.
 */
export async function sendMessageToAI(
  jid: string,
  message: string,
  options: MessageOptions
): Promise<string | null> {
  const { conversationType, senderJid, senderName, saveOnly, messageId, image, document } = options;

  if (saveOnly) {
    logger.info({ jid, saveOnly, conversationType }, 'Saving message only');

    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/chat/save`,
      {
        method: 'POST',
        headers: aiApiHeaders('application/json'),
        body: JSON.stringify({
          whatsapp_jid: jid,
          message,
          sender_jid: senderJid,
          sender_name: senderName,
          conversation_type: conversationType,
          whatsapp_message_id: messageId,
        }),
      },
      config.timeouts.default
    );

    if (!response.ok) {
      throw new Error(`AI API error: ${response.status} ${response.statusText}`);
    }
    return '';
  }

  logger.info(
    { jid, conversationType, messageId, hasImage: !!image, hasDocument: !!document },
    'Enqueuing message to AI API'
  );

  const requestBody: Record<string, unknown> = {
    whatsapp_jid: jid,
    message,
    sender_jid: senderJid,
    sender_name: senderName,
    conversation_type: conversationType,
    whatsapp_message_id: messageId,
    client_id: 'telegram',
  };

  if (options.isGroupAdmin !== undefined) {
    requestBody.is_group_admin = options.isGroupAdmin;
  }

  if (image) {
    requestBody.image_data = image.data;
    requestBody.image_mimetype = image.mimetype;
  }

  if (document) {
    requestBody.document_data = document.data;
    requestBody.document_mimetype = document.mimetype;
    requestBody.document_filename = document.filename;
  }

  const enqueueResponse = await fetchWithTimeout(
    `${config.aiApiUrl}/chat/enqueue`,
    {
      method: 'POST',
      headers: aiApiHeaders('application/json'),
      body: JSON.stringify(requestBody),
    },
    config.timeouts.default
  );

  if (!enqueueResponse.ok) {
    if (enqueueResponse.status === 403) {
      logger.warn({ jid }, 'Message blocked by API whitelist');
      return null;
    }
    throw new Error(`Enqueue failed: ${enqueueResponse.status} ${enqueueResponse.statusText}`);
  }

  const enqueueResult: EnqueueResponse = await enqueueResponse.json();

  if (enqueueResult.is_command) {
    logger.info({ jid }, 'Command executed');
    return enqueueResult.response || '';
  }

  const job_id = enqueueResult.job_id;
  if (!job_id) {
    throw new Error('No job_id in enqueue response');
  }

  logger.info({ job_id }, 'Job enqueued, polling for completion');

  const startTime = Date.now();
  let iterations = 0;
  const endTimer = apiPollDuration.startTimer();
  let pollStatus: 'success' | 'error' = 'error';

  try {
    while (iterations < config.polling.maxIterations) {
      const elapsedMs = Date.now() - startTime;
      if (elapsedMs >= config.polling.maxDurationMs) {
        throw new Error(
          `Polling timeout: job ${job_id} exceeded ${config.polling.maxDurationMs}ms`
        );
      }

      const statusResponse = await fetchWithTimeout(
        `${config.aiApiUrl}/chat/job/${job_id}`,
        { headers: aiApiHeaders() },
        config.timeouts.polling
      );

      if (!statusResponse.ok) {
        throw new Error(`Job status failed: ${statusResponse.status} ${statusResponse.statusText}`);
      }

      const status: JobStatus = await statusResponse.json();

      if (status.complete) {
        logger.info({ job_id, iterations, elapsedMs }, 'Job completed');
        pollStatus = 'success';
        return status.full_response || '';
      }

      iterations++;
      await new Promise((resolve) => setTimeout(resolve, config.polling.intervalMs));
    }

    throw new Error(
      `Polling timeout: job ${job_id} exceeded ${config.polling.maxIterations} iterations`
    );
  } finally {
    endTimer({ status: pollStatus });
  }
}

export async function getUserPreferences(jid: string): Promise<UserPreferences | null> {
  try {
    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/preferences/${encodeURIComponent(jid)}`,
      { headers: aiApiHeaders() },
      config.timeouts.default
    );
    if (!response.ok) {
      logger.debug({ jid, status: response.status }, 'Failed to fetch preferences');
      return null;
    }
    return response.json();
  } catch (error) {
    logger.warn({ jid, error }, 'Error fetching user preferences');
    return null;
  }
}

export async function textToSpeech(text: string, jid: string): Promise<Buffer | null> {
  try {
    logger.info({ jid, textLength: text.length }, 'Requesting TTS');

    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/tts`,
      {
        method: 'POST',
        headers: aiApiHeaders('application/json'),
        body: JSON.stringify({ text, whatsapp_jid: jid }),
      },
      config.timeouts.tts
    );

    if (!response.ok) {
      logger.warn({ jid, status: response.status }, 'TTS request failed');
      return null;
    }

    const arrayBuffer = await response.arrayBuffer();
    logger.info({ jid, audioSize: arrayBuffer.byteLength }, 'TTS audio received');
    return Buffer.from(arrayBuffer);
  } catch (error) {
    logger.error({ jid, error }, 'Error generating TTS');
    return null;
  }
}

/**
 * Download an audio buffer from Telegram and POST it to the AI API's
 * /transcribe endpoint. Returns the transcription text or null.
 */
export async function transcribeAudio(
  buffer: Buffer,
  mimetype: string,
  filename: string
): Promise<string | null> {
  try {
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
        const errorData = (await response.json()) as { detail?: string };
        throw new Error(errorData.detail || 'Transcription failed');
      }
      const text = await response.text();
      logger.error({ status: response.status, body: text }, 'Unexpected transcription error');
      throw new Error(`Transcription failed with status ${response.status}`);
    }

    const result = (await response.json()) as { transcription?: string };
    return result.transcription || null;
  } catch (error) {
    logger.error({ error }, 'Error transcribing audio');
    return null;
  }
}
