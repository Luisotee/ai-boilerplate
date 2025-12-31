import { logger } from './logger.js';

const AI_API_URL = process.env.AI_API_URL || 'http://localhost:8000';

interface MessageOptions {
  conversationType: 'private' | 'group';  // Conversation type (required)
  senderJid?: string;
  senderName?: string;
  saveOnly?: boolean;  // Only save, don't get AI response
}

export async function sendMessageToAI(
  whatsappJid: string,
  message: string,
  options: MessageOptions
): Promise<AsyncIterable<string>> {

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

    // Return empty iterator for save-only requests
    return (async function*() {})();
  }

  // Step 1: POST to /chat/enqueue to get job_id
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
  logger.info({ job_id }, 'Job enqueued, starting stream');

  // Step 2: Stream from /chat/stream/{job_id}
  const streamResponse = await fetch(`${AI_API_URL}/chat/stream/${job_id}`, {
    headers: { 'Accept': 'text/event-stream' },
  });

  if (!streamResponse.ok) {
    throw new Error(`Stream failed: ${streamResponse.status} ${streamResponse.statusText}`);
  }

  if (!streamResponse.body) {
    throw new Error('No response body from stream endpoint');
  }

  return parseJobSSE(streamResponse.body);
}

async function* parseJobSSE(body: ReadableStream<Uint8Array>): AsyncIterable<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6);

          // Handle special SSE signals
          if (data === '[DONE]') {
            logger.info('Stream completed successfully');
            return;
          }
          if (data === '[ERROR]') {
            logger.error('Stream error received from worker');
            throw new Error('Worker processing failed');
          }

          try {
            // Parse chunk object: {index, content, timestamp}
            const chunk = JSON.parse(data);
            yield chunk.content;  // Yield only the content string
          } catch (e) {
            logger.warn({ data, error: e }, 'Failed to parse chunk JSON');
            // Don't yield unparseable data
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
