import { config } from './config.js';
import { logger } from './logger.js';

const POLL_INTERVAL_MS = 500;

interface MessageOptions {
  conversationType: 'private' | 'group';
  senderJid?: string;
  senderName?: string;
  saveOnly?: boolean;
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
  const { conversationType, senderJid, senderName, saveOnly } = options;

  // Handle save-only endpoint separately
  if (saveOnly) {
    logger.info({ whatsappJid, saveOnly, conversationType }, 'Saving message only');

    const response = await fetch(`${config.aiApiUrl}/chat/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        whatsapp_jid: whatsappJid,
        message,
        sender_jid: senderJid,
        sender_name: senderName,
        conversation_type: conversationType,
      }),
    });

    if (!response.ok) {
      throw new Error(`AI API error: ${response.status} ${response.statusText}`);
    }

    return '';
  }

  // Step 1: Enqueue message
  logger.info({ whatsappJid, conversationType }, 'Enqueuing message to AI API');

  const enqueueResponse = await fetch(`${config.aiApiUrl}/chat/enqueue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      whatsapp_jid: whatsappJid,
      message,
      sender_jid: senderJid,
      sender_name: senderName,
      conversation_type: conversationType,
    }),
  });

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
  while (true) {
    const statusResponse = await fetch(`${config.aiApiUrl}/chat/job/${job_id}`);

    if (!statusResponse.ok) {
      throw new Error(`Job status failed: ${statusResponse.status} ${statusResponse.statusText}`);
    }

    const status: JobStatus = await statusResponse.json();

    if (status.complete) {
      logger.info({ job_id }, 'Job completed');
      return status.full_response || '';
    }

    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

export async function getUserPreferences(whatsappJid: string): Promise<UserPreferences | null> {
  try {
    const response = await fetch(
      `${config.aiApiUrl}/preferences/${encodeURIComponent(whatsappJid)}`
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

    const response = await fetch(`${config.aiApiUrl}/tts`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, whatsapp_jid: whatsappJid }),
    });

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
