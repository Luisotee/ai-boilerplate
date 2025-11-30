import { logger } from './logger.js';

const AI_API_URL = process.env.AI_API_URL || 'http://localhost:8000';

export async function sendMessageToAI(phone: string, message: string): Promise<AsyncIterable<string>> {
  const response = await fetch(`${AI_API_URL}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
    },
    body: JSON.stringify({ phone, message }),
  });

  if (!response.ok) {
    throw new Error(`AI API error: ${response.status} ${response.statusText}`);
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
          yield data;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
