import { describe, it, expect, vi, beforeEach } from 'vitest';
import { makeTextMsg, makeGroupTextMsg, makeMockSocket } from '../helpers/fixtures.js';

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

vi.mock('../../src/logger.js', () => ({
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

vi.mock('../../src/utils/message-split.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/utils/message-split.js')>();
  return {
    ...actual,
    sleep: vi.fn().mockResolvedValue(undefined), // skip real delays
  };
});

import { handleTextMessage } from '../../src/handlers/text.js';
import { sendMessageToAI } from '../../src/api-client.js';
import { sleep } from '../../src/utils/message-split.js';
import { logger } from '../../src/logger.js';

const mockSendMessageToAI = sendMessageToAI as ReturnType<typeof vi.fn>;
const mockSleep = sleep as ReturnType<typeof vi.fn>;
const mockLoggerWarn = logger.warn as unknown as ReturnType<typeof vi.fn>;
const mockLoggerInfo = logger.info as unknown as ReturnType<typeof vi.fn>;

describe('handleTextMessage — burst sending', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSplitConfig.enabled = true;
    mockSplitConfig.baseDelayMs = 600;
    mockSplitConfig.perCharMs = 25;
    mockSplitConfig.maxDelayMs = 3500;
    mockSplitConfig.maxChunks = 5;
  });

  it('sends a single message when the response has no delimiters', async () => {
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce('Hello, just one thought.');

    await handleTextMessage(sock as never, msg as never, 'hey');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    expect(textCalls).toHaveLength(1);
    expect(textCalls[0][1]).toEqual({ text: 'Hello, just one thought.' });
    expect(mockSleep).not.toHaveBeenCalled();
  });

  it('sends multiple messages with a composing presence and sleep between each', async () => {
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce(
      'Hey, how are you?\n---\nWanna go out tonight?\n---\nGet some pizza.'
    );

    await handleTextMessage(sock as never, msg as never, 'hey');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    expect(textCalls.map(([, payload]) => (payload as { text: string }).text)).toEqual([
      'Hey, how are you?',
      'Wanna go out tonight?',
      'Get some pizza.',
    ]);

    // Presence calls: initial 'composing' + 2 between chunks + final 'paused' in finally
    const presenceStates = sock.sendPresenceUpdate.mock.calls.map(([state]) => state);
    expect(presenceStates.filter((s) => s === 'composing')).toHaveLength(3);
    expect(presenceStates).toContain('paused');

    // Sleep fires between chunks (2 gaps for 3 chunks)
    expect(mockSleep).toHaveBeenCalledTimes(2);
  });

  it('merges group-chat messages into a single delimiter-free message', async () => {
    const sock = makeMockSocket();
    const msg = makeGroupTextMsg('hey @bot');
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond');

    await handleTextMessage(sock as never, msg as never, 'hey @bot');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    expect(textCalls).toHaveLength(1);
    expect(textCalls[0][1]).toEqual({ text: 'first\n\nsecond' });
    expect(mockSleep).not.toHaveBeenCalled();
  });

  it('strips delimiters before sending the response to TTS', async () => {
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    const { getUserPreferences, textToSpeech } = await import('../../src/api-client.js');
    const mockPrefs = getUserPreferences as ReturnType<typeof vi.fn>;
    const mockTts = textToSpeech as ReturnType<typeof vi.fn>;
    mockPrefs.mockResolvedValueOnce({ tts_enabled: true });
    mockTts.mockResolvedValueOnce(Buffer.from('fake-audio'));

    mockSendMessageToAI.mockResolvedValueOnce('First.\n---\nSecond.');

    await handleTextMessage(sock as never, msg as never, 'hey');

    expect(mockTts).toHaveBeenCalledTimes(1);
    expect(mockTts.mock.calls[0][0]).toBe('First.\n\nSecond.');
  });

  it('sends a single delimiter-free message when MESSAGE_SPLIT_ENABLED is disabled', async () => {
    mockSplitConfig.enabled = false;
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond\n---\nthird');

    await handleTextMessage(sock as never, msg as never, 'hey');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    expect(textCalls).toHaveLength(1);
    expect(textCalls[0][1]).toEqual({ text: 'first\n\nsecond\n\nthird' });
    expect(mockSleep).not.toHaveBeenCalled();
  });

  it('passes the correct delay (base + per-char × next chunk length) to sleep', async () => {
    mockSplitConfig.baseDelayMs = 100;
    mockSplitConfig.perCharMs = 10;
    mockSplitConfig.maxDelayMs = 10_000;
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nabcdef');

    await handleTextMessage(sock as never, msg as never, 'hey');

    expect(mockSleep).toHaveBeenCalledTimes(1);
    expect(mockSleep.mock.calls[0][0]).toBe(100 + 6 * 10);
  });

  it('clamps inter-chunk delay to maxDelayMs for long chunks', async () => {
    mockSplitConfig.baseDelayMs = 500;
    mockSplitConfig.perCharMs = 25;
    mockSplitConfig.maxDelayMs = 1000;
    const longChunk = 'a'.repeat(500);
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce(`first\n---\n${longChunk}`);

    await handleTextMessage(sock as never, msg as never, 'hey');

    expect(mockSleep).toHaveBeenCalledTimes(1);
    expect(mockSleep.mock.calls[0][0]).toBe(1000);
  });

  it('does not send outbound messages when AI returns null', async () => {
    const sock = makeMockSocket();
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce(null);

    await handleTextMessage(sock as never, msg as never, 'hey');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    expect(textCalls).toHaveLength(0);
  });

  it('delivers partial burst, swallows mid-stream send error, and logs warn (not info)', async () => {
    const sock = makeMockSocket();
    sock.sendMessage
      .mockResolvedValueOnce({ key: { id: 'SENT_1' } })
      .mockRejectedValueOnce(new Error('send broke'));
    const msg = makeTextMsg('hey');
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond\n---\nthird');

    await handleTextMessage(sock as never, msg as never, 'hey');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    // 1 delivered, 1 attempted-and-failed; we never try the 3rd
    expect(textCalls).toHaveLength(2);
    expect((textCalls[0][1] as { text: string }).text).toBe('first');
    expect((textCalls[1][1] as { text: string }).text).toBe('second');

    // No error reaction was sent (outer catch should not have fired)
    const errorReplies = sock.sendMessage.mock.calls.filter(([, payload]) => {
      const text = (payload as { text?: string }).text;
      return typeof text === 'string' && text.startsWith('Sorry, I encountered an error');
    });
    expect(errorReplies).toHaveLength(0);

    // Both the mid-stream warn and the summary warn fired; no success info log.
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
