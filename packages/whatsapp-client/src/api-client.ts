import { logger } from './logger.js';

const AI_API_URL = process.env.AI_API_URL || 'http://localhost:8000';

interface MessageOptions {
  senderJid?: string;
  senderName?: string;
  saveOnly?: boolean;  // Only save, don't get AI response
}

export async function sendMessageToAI(
  whatsappJid: string,
  message: string,
  options: MessageOptions = {}
): Promise<AsyncIterable<string>> {

  const { senderJid, senderName, saveOnly } = options;

  // Use save-only endpoint if requested
  const endpoint = saveOnly ? '/chat/save' : '/chat/stream';

  const response = await fetch(`${AI_API_URL}${endpoint}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': saveOnly ? 'application/json' : 'text/event-stream',
    },
    body: JSON.stringify({
      whatsapp_jid: whatsappJid,
      message,
      sender_jid: senderJid,
      sender_name: senderName,
    }),
  });

  if (!response.ok) {
    throw new Error(`AI API error: ${response.status} ${response.statusText}`);
  }

  if (saveOnly) {
    // Return empty iterator for save-only requests
    return (async function*() {})();
  }

  if (!response.body) {
    throw new Error('No response body from AI API');
  }

  return parseSSE(response.body);
}

async function* parseSSE(body: ReadableStream<Uint8Array>): AsyncIterable<string> {
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
          if (data === '[DONE]') return;
          if (data === '[ERROR]') {
            logger.error('Stream error received from API');
            return;
          }
          yield data;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
