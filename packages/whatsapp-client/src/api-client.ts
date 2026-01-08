import { logger } from './logger.js';

const AI_API_URL = process.env.AI_API_URL || 'http://localhost:8000';
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

export async function sendMessageToAI(
  whatsappJid: string,
  message: string,
  options: MessageOptions
): Promise<string> {
  const { conversationType, senderJid, senderName, saveOnly } = options;

  // Handle save-only endpoint separately
  if (saveOnly) {
    logger.info({ whatsappJid, saveOnly, conversationType }, 'Saving message only');

    const response = await fetch(`${AI_API_URL}/chat/save`, {
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

  const enqueueResponse = await fetch(`${AI_API_URL}/chat/enqueue`, {
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

  const { job_id } = await enqueueResponse.json();
  logger.info({ job_id }, 'Job enqueued, polling for completion');

  // Step 2: Poll until complete
  while (true) {
    const statusResponse = await fetch(`${AI_API_URL}/chat/job/${job_id}`);

    if (!statusResponse.ok) {
      throw new Error(`Job status failed: ${statusResponse.status} ${statusResponse.statusText}`);
    }

    const status: JobStatus = await statusResponse.json();

    if (status.complete) {
      logger.info({ job_id }, 'Job completed');
      return status.full_response || '';
    }

    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}
