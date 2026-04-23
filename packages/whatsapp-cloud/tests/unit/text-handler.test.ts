import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockSplitConfig = vi.hoisted(() => ({
  enabled: true,
  baseDelayMs: 600,
  perCharMs: 25,
  maxDelayMs: 3500,
  maxChunks: 5,
}));

vi.mock('../../src/config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/config.js')>();
  return {
    config: {
      ...actual.config,
      messageSplit: mockSplitConfig,
    },
  };
});

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

vi.mock('../../src/logger.js', () => ({
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

import { handleTextMessage } from '../../src/handlers/text.js';
import { sendMessageToAI } from '../../src/api-client.js';
import * as graphApi from '../../src/services/graph-api.js';
import { sleep } from '../../src/utils/message-split.js';
import { logger } from '../../src/logger.js';

const mockSendMessageToAI = sendMessageToAI as ReturnType<typeof vi.fn>;
const mockSendText = graphApi.sendText as ReturnType<typeof vi.fn>;
const mockTyping = graphApi.sendTypingIndicator as ReturnType<typeof vi.fn>;
const mockLoggerWarn = logger.warn as unknown as ReturnType<typeof vi.fn>;
const mockLoggerInfo = logger.info as unknown as ReturnType<typeof vi.fn>;
const mockReaction = graphApi.sendReaction as ReturnType<typeof vi.fn>;
const mockSleep = sleep as ReturnType<typeof vi.fn>;

describe('handleTextMessage (Cloud) — burst sending', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSplitConfig.enabled = true;
    mockSplitConfig.baseDelayMs = 600;
    mockSplitConfig.perCharMs = 25;
    mockSplitConfig.maxDelayMs = 3500;
    mockSplitConfig.maxChunks = 5;
  });

  it('sends a single message when the response has no delimiters', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('Hello, single thought.');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText).toHaveBeenCalledTimes(1);
    expect(mockSendText.mock.calls[0]).toEqual(['16505551234', 'Hello, single thought.']);
    expect(mockSleep).not.toHaveBeenCalled();
  });

  it('sends multiple messages with a sleep between each and does not fire typing from the handler', async () => {
    mockSendMessageToAI.mockResolvedValueOnce(
      'Hey, how are you?\n---\nWanna go out tonight?\n---\nGet some pizza.'
    );

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText.mock.calls.map(([, body]) => body)).toEqual([
      'Hey, how are you?',
      'Wanna go out tonight?',
      'Get some pizza.',
    ]);
    // Typing indicator is fired by the webhook router (before dispatch), not by
    // this handler — and Meta's one-shot-per-wamid rule means it can't be re-fired.
    expect(mockTyping).not.toHaveBeenCalled();
    // Sleep fires between chunks only (2 gaps for 3 chunks).
    expect(mockSleep).toHaveBeenCalledTimes(2);
  });

  it('merges group-chat messages into a single delimiter-free message', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User', undefined, undefined, {
      conversationType: 'group',
    });

    expect(mockSendText).toHaveBeenCalledTimes(1);
    expect(mockSendText.mock.calls[0][1]).toBe('first\n\nsecond');
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

  it('sends a single delimiter-free message when MESSAGE_SPLIT_ENABLED is disabled', async () => {
    mockSplitConfig.enabled = false;
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond\n---\nthird');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText).toHaveBeenCalledTimes(1);
    expect(mockSendText.mock.calls[0][1]).toBe('first\n\nsecond\n\nthird');
    expect(mockSleep).not.toHaveBeenCalled();
    expect(mockTyping).not.toHaveBeenCalled();
  });

  it('passes the correct delay (base + per-char × next chunk length) to sleep', async () => {
    mockSplitConfig.baseDelayMs = 100;
    mockSplitConfig.perCharMs = 10;
    mockSplitConfig.maxDelayMs = 10_000;
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nabcdef');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSleep).toHaveBeenCalledTimes(1);
    expect(mockSleep.mock.calls[0][0]).toBe(100 + 6 * 10);
  });

  it('clamps inter-chunk delay to maxDelayMs for long chunks', async () => {
    mockSplitConfig.baseDelayMs = 500;
    mockSplitConfig.perCharMs = 25;
    mockSplitConfig.maxDelayMs = 1000;
    const longChunk = 'a'.repeat(500);
    mockSendMessageToAI.mockResolvedValueOnce(`first\n---\n${longChunk}`);

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSleep).toHaveBeenCalledTimes(1);
    expect(mockSleep.mock.calls[0][0]).toBe(1000);
  });

  it('does not send outbound messages when AI returns empty string', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText).not.toHaveBeenCalled();
  });

  it('does not send failure reaction when TTS fails after text was delivered', async () => {
    const { getUserPreferences, textToSpeech } = await import('../../src/api-client.js');
    const mockPrefs = getUserPreferences as ReturnType<typeof vi.fn>;
    const mockTts = textToSpeech as ReturnType<typeof vi.fn>;
    mockPrefs.mockResolvedValueOnce({ tts_enabled: true });
    mockTts.mockRejectedValueOnce(new Error('TTS broke'));

    mockSendMessageToAI.mockResolvedValueOnce('First.\n---\nSecond.');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText).toHaveBeenCalledTimes(2);
    expect(mockReaction).not.toHaveBeenCalled();
  });

  it('delivers partial burst, swallows mid-stream send error, and logs warn (not info)', async () => {
    mockSendText
      .mockResolvedValueOnce('wamid.sent.1')
      .mockRejectedValueOnce(new Error('send broke'));
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond\n---\nthird');

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    // 1 delivered, 1 attempted-and-failed; we never try the 3rd
    expect(mockSendText).toHaveBeenCalledTimes(2);
    expect(mockSendText.mock.calls[0][1]).toBe('first');
    expect(mockSendText.mock.calls[1][1]).toBe('second');

    // No failure reaction fired (outer catch should not have triggered).
    expect(mockReaction).not.toHaveBeenCalled();

    const burstWarn = mockLoggerWarn.mock.calls.find(
      ([, message]) => message === 'Burst send failed mid-stream; partial response delivered'
    );
    expect(burstWarn).toBeDefined();
    const partialSummary = mockLoggerWarn.mock.calls.find(
      ([fields, message]) =>
        message === 'Partially sent AI response' &&
        (fields as { sentCount: number }).sentCount === 1 &&
        (fields as { chunkCount: number }).chunkCount === 3
    );
    expect(partialSummary).toBeDefined();
    const successInfo = mockLoggerInfo.mock.calls.find(
      ([, message]) => message === 'Sent AI response'
    );
    expect(successInfo).toBeUndefined();
  });
});
