import { describe, it, expect, vi, beforeEach } from 'vitest';
import { makeTextMsg, makeGroupTextMsg, makeMockSocket } from '../helpers/fixtures.js';

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(),
  getUserPreferences: vi.fn().mockResolvedValue({ tts_enabled: false }),
  textToSpeech: vi.fn(),
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

const mockSendMessageToAI = sendMessageToAI as ReturnType<typeof vi.fn>;
const mockSleep = sleep as ReturnType<typeof vi.fn>;

describe('handleTextMessage — burst sending', () => {
  beforeEach(() => {
    vi.clearAllMocks();
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

  it('does not split group-chat messages even when delimiters are present', async () => {
    const sock = makeMockSocket();
    const msg = makeGroupTextMsg('hey @bot');
    mockSendMessageToAI.mockResolvedValueOnce('first\n---\nsecond');

    await handleTextMessage(sock as never, msg as never, 'hey @bot');

    const textCalls = sock.sendMessage.mock.calls.filter(
      ([, payload]) => 'text' in (payload as { text?: string })
    );
    expect(textCalls).toHaveLength(1);
    expect(textCalls[0][1]).toEqual({ text: 'first\n---\nsecond' });
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
});
