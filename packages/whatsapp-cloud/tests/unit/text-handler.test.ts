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

vi.mock('../../src/api-client.js', async (importOriginal) => ({
  // Real error class + notice: the handler tells a poll timeout from a failure.
  PollTimeoutError: (await importOriginal<typeof import('../../src/api-client.js')>())
    .PollTimeoutError,
  LATE_REPLY_NOTICE: (await importOriginal<typeof import('../../src/api-client.js')>())
    .LATE_REPLY_NOTICE,
  sendMessageToAI: vi.fn(),
  fetchJobMedia: vi.fn(),
  getUserPreferences: vi.fn().mockResolvedValue({ tts_enabled: false }),
  textToSpeech: vi.fn(),
}));

vi.mock('../../src/services/graph-api.js', () => ({
  sendText: vi.fn().mockResolvedValue('wamid.sent'),
  sendTypingIndicator: vi.fn().mockResolvedValue(undefined),
  sendAudio: vi.fn().mockResolvedValue('wamid.audio'),
  sendImage: vi.fn().mockResolvedValue('wamid.image'),
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

describe('handleTextMessage (Cloud) — reply outlives the polling window', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('sends the late-reply notice and no ❌', async () => {
    const { PollTimeoutError, LATE_REPLY_NOTICE } = await import('../../src/api-client.js');
    mockSendMessageToAI.mockRejectedValueOnce(new PollTimeoutError('job-1', '120000ms'));

    await handleTextMessage('16505551234', 'wamid.abc', 'hi', 'Test User');

    expect(mockSendText).toHaveBeenCalledTimes(1);
    expect(mockSendText.mock.calls[0][1]).toBe(LATE_REPLY_NOTICE);
    expect(mockReaction).not.toHaveBeenCalled();
  });
});
describe('handleTextMessage (Cloud) — reply images', () => {
  const media = [
    { index: 0, caption: 'Fire map', mimetype: 'image/png', url: 'https://m.example.com/a.png' },
  ];

  beforeEach(() => vi.clearAllMocks());

  async function setup(response: string) {
    const { fetchJobMedia } = await import('../../src/api-client.js');
    (fetchJobMedia as ReturnType<typeof vi.fn>).mockResolvedValue(Buffer.from('png-bytes'));
    mockSendMessageToAI.mockImplementationOnce(
      async (_jid: string, _text: string, options: { onMedia?: (...a: unknown[]) => void }) => {
        options.onMedia?.('job-9', media);
        return response;
      }
    );
  }

  it('sends the text, then uploads the image with its caption', async () => {
    await setup('Here is the map.');

    await handleTextMessage('16505551234', 'wamid.abc', 'map?', 'Test User');

    expect(mockSendText.mock.calls).toEqual([['16505551234', 'Here is the map.']]);
    expect(graphApi.sendImage).toHaveBeenCalledWith(
      '16505551234',
      Buffer.from('png-bytes'),
      'image/png',
      'Fire map'
    );
  });

  it('sends no empty text message for an images-only reply', async () => {
    await setup('');

    await handleTextMessage('16505551234', 'wamid.abc', 'map?', 'Test User');

    expect(mockSendText).not.toHaveBeenCalled();
    expect(graphApi.sendImage).toHaveBeenCalledTimes(1);
    expect(mockReaction).not.toHaveBeenCalled();
  });

  it('falls back to the link as text when the upload fails', async () => {
    await setup('Map:');
    (graphApi.sendImage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('upload'));

    await handleTextMessage('16505551234', 'wamid.abc', 'map?', 'Test User');

    expect(mockSendText.mock.calls).toEqual([
      ['16505551234', 'Map:'],
      ['16505551234', 'Fire map: https://m.example.com/a.png'],
    ]);
    expect(mockReaction).not.toHaveBeenCalled();
  });
});
