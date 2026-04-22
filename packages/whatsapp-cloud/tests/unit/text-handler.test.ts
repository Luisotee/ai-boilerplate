import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(),
  getUserPreferences: vi.fn().mockResolvedValue({ tts_enabled: false }),
  textToSpeech: vi.fn(),
}));

vi.mock('../../src/services/graph-api.js', () => ({
  sendText: vi.fn().mockResolvedValue('wamid.sent'),
  sendTypingIndicator: vi.fn().mockResolvedValue(undefined),
  sendAudio: vi.fn().mockResolvedValue('wamid.audio'),
  sendReaction: vi.fn().mockResolvedValue(undefined),
  markAsRead: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../../src/utils/message-split.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/utils/message-split.js')>();
  return {
    ...actual,
    sleep: vi.fn().mockResolvedValue(undefined),
  };
});

import { handleTextMessage } from '../../src/handlers/text.js';
import { sendMessageToAI } from '../../src/api-client.js';
import * as graphApi from '../../src/services/graph-api.js';
import { sleep } from '../../src/utils/message-split.js';

const mockSendMessageToAI = sendMessageToAI as ReturnType<typeof vi.fn>;
const mockSendText = graphApi.sendText as ReturnType<typeof vi.fn>;
const mockTyping = graphApi.sendTypingIndicator as ReturnType<typeof vi.fn>;
const mockSleep = sleep as ReturnType<typeof vi.fn>;

describe('handleTextMessage (Cloud) — burst sending', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('sends a single message when the response has no delimiters', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('Hello, single thought.');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText).toHaveBeenCalledTimes(1);
    expect(mockSendText.mock.calls[0]).toEqual(['16505551234', 'Hello, single thought.']);
    expect(mockSleep).not.toHaveBeenCalled();
  });

  it('sends multiple messages with a sleep between each and does not re-fire typing', async () => {
    mockSendMessageToAI.mockResolvedValueOnce(
      'Hey, how are you?\n---\nWanna go out tonight?\n---\nGet some pizza.'
    );

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText.mock.calls.map(([, body]) => body)).toEqual([
      'Hey, how are you?',
      'Wanna go out tonight?',
      'Get some pizza.',
    ]);
    // Typing indicator fires exactly once (at the start); never re-fired per chunk.
    expect(mockTyping).toHaveBeenCalledTimes(1);
    // Sleep fires between chunks only (2 gaps for 3 chunks).
    expect(mockSleep).toHaveBeenCalledTimes(2);
  });

  it('does not split group-chat messages even when delimiters are present', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User', undefined, undefined, {
      conversationType: 'group',
    });

    expect(mockSendText).toHaveBeenCalledTimes(1);
    expect(mockSendText.mock.calls[0][1]).toBe('first\n---\nsecond');
    expect(mockSleep).not.toHaveBeenCalled();
  });

  it('strips delimiters before sending the response to TTS', async () => {
    const { getUserPreferences, textToSpeech } = await import('../../src/api-client.js');
    const mockPrefs = getUserPreferences as ReturnType<typeof vi.fn>;
    const mockTts = textToSpeech as ReturnType<typeof vi.fn>;
    mockPrefs.mockResolvedValueOnce({ tts_enabled: true });
    mockTts.mockResolvedValueOnce(Buffer.from('fake-audio'));

    mockSendMessageToAI.mockResolvedValueOnce('First.\n---\nSecond.');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockTts).toHaveBeenCalledTimes(1);
    expect(mockTts.mock.calls[0][0]).toBe('First.\n\nSecond.');
  });
});
