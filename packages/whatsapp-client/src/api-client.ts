import { config } from './config.js';
import { logger } from './logger.js';
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
  // Regular job response
  job_id?: string;
  status?: string;
  message?: string;
  // Command response
  is_command?: boolean;
  response?: string;
}

interface UserPreferences {
  tts_enabled: boolean;
  tts_language: string;
  stt_language: string | null;
}

export async function sendMessageToAI(
  whatsappJid: string,
  message: string,
  options: MessageOptions
): Promise<string> {
  const {
    conversationType,
    senderJid,
    senderName,
    saveOnly,
    messageId,
    image,
    document,
    isGroupAdmin,
  } = options;

  // Handle save-only endpoint separately
  if (saveOnly) {
    logger.info({ whatsappJid, saveOnly, conversationType }, 'Saving message only');

    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/chat/save`,
      {
        method: 'POST',
        headers: aiApiHeaders('application/json'),
        body: JSON.stringify({
          whatsapp_jid: whatsappJid,
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

  // Step 1: Enqueue message
  logger.info(
    { whatsappJid, conversationType, messageId, hasImage: !!image, hasDocument: !!document },
    'Enqueuing message to AI API'
  );

  const requestBody: Record<string, unknown> = {
    whatsapp_jid: whatsappJid,
    message,
    sender_jid: senderJid,
    sender_name: senderName,
    conversation_type: conversationType,
    whatsapp_message_id: messageId,
  };

  // Add group admin status if available (for admin-only commands)
  if (isGroupAdmin !== undefined) {
    requestBody.is_group_admin = isGroupAdmin;
  }

  // Add image data if present
  if (image) {
    requestBody.image_data = image.data;
    requestBody.image_mimetype = image.mimetype;
  }

  // Add document data if present
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
    throw new Error(`Enqueue failed: ${enqueueResponse.status} ${enqueueResponse.statusText}`);
  }

  const enqueueResult: EnqueueResponse = await enqueueResponse.json();

  // Handle command response (e.g., /settings, /tts on, /help)
  if (enqueueResult.is_command) {
    logger.info({ whatsappJid }, 'Command executed');
    return enqueueResult.response || '';
  }

  const job_id = enqueueResult.job_id;
  if (!job_id) {
    throw new Error('No job_id in enqueue response');
  }

  logger.info({ job_id }, 'Job enqueued, polling for completion');

  // Step 2: Poll until complete
  const startTime = Date.now();
  let iterations = 0;

  while (iterations < config.polling.maxIterations) {
    const elapsedMs = Date.now() - startTime;
    if (elapsedMs >= config.polling.maxDurationMs) {
      throw new Error(`Polling timeout: job ${job_id} exceeded ${config.polling.maxDurationMs}ms`);
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
      return status.full_response || '';
    }

    iterations++;
    await new Promise((resolve) => setTimeout(resolve, config.polling.intervalMs));
  }

  throw new Error(
    `Polling timeout: job ${job_id} exceeded ${config.polling.maxIterations} iterations`
  );
}

export async function getUserPreferences(whatsappJid: string): Promise<UserPreferences | null> {
  try {
    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/preferences/${encodeURIComponent(whatsappJid)}`,
      { headers: aiApiHeaders() },
      config.timeouts.default
    );
    if (!response.ok) {
      logger.debug({ whatsappJid, status: response.status }, 'Failed to fetch preferences');
      return null;
    }
    return response.json();
  } catch (error) {
    logger.warn({ whatsappJid, error }, 'Error fetching user preferences');
    return null;
  }
}

export async function textToSpeech(text: string, whatsappJid: string): Promise<Buffer | null> {
  try {
    logger.info({ whatsappJid, textLength: text.length }, 'Requesting TTS');

    const response = await fetchWithTimeout(
      `${config.aiApiUrl}/tts`,
      {
        method: 'POST',
        headers: aiApiHeaders('application/json'),
        body: JSON.stringify({ text, whatsapp_jid: whatsappJid }),
      },
      config.timeouts.tts
    );

    if (!response.ok) {
      logger.warn({ whatsappJid, status: response.status }, 'TTS request failed');
      return null;
    }

    const arrayBuffer = await response.arrayBuffer();
    logger.info({ whatsappJid, audioSize: arrayBuffer.byteLength }, 'TTS audio received');
    return Buffer.from(arrayBuffer);
  } catch (error) {
    logger.error({ whatsappJid, error }, 'Error generating TTS');
    return null;
  }
}
