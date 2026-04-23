/**
 * Unit tests for handlers/text.ts handleTextMessage.
 *
 * Covers the orchestrator's main branches:
 *   - missing chat/message context → early return
 *   - saveOnly → persist and return without replying
 *   - happy path → ctx.reply + messagesSent increment
 *   - AI error → sendReaction('❌') AND ctx.reply (the H2 parity fix)
 *   - reaction failure does not prevent the error reply
 *   - TTS enabled → replyWithVoice
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(),
  getUserPreferences: vi.fn(),
  textToSpeech: vi.fn(),
}));

vi.mock('../../src/services/telegram-api.js', () => ({
  sendReaction: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../../src/config.js', () => ({
  config: {
    aiApiUrl: 'http://localhost:8000',
    logLevel: 'silent',
    telegramApiKey: 'test',
    aiApiKey: 'test',
    server: { port: 3003, host: '0.0.0.0' },
    corsOrigins: '',
    rateLimitGlobal: 30,
    rateLimitExpensive: 5,
    timeouts: { default: 30000, transcription: 60000, tts: 45000, polling: 5000 },
    polling: { intervalMs: 500, maxIterations: 240, maxDurationMs: 120000 },
    telegram: {
      botToken: 'test',
      webhookSecret: '',
      publicWebhookUrl: '',
      dropPendingUpdates: true,
    },
    messageSplit: {
      enabled: true,
      baseDelayMs: 0,
      perCharMs: 0,
      maxDelayMs: 0,
      maxChunks: 5,
    },
    whitelistPhones: new Set<string>(),
  },
}));

import { handleTextMessage } from '../../src/handlers/text.js';
import * as apiClient from '../../src/api-client.js';
import * as telegramApi from '../../src/services/telegram-api.js';

const mockSendMessageToAI = apiClient.sendMessageToAI as ReturnType<typeof vi.fn>;
const mockGetUserPreferences = apiClient.getUserPreferences as ReturnType<typeof vi.fn>;
const mockTextToSpeech = apiClient.textToSpeech as ReturnType<typeof vi.fn>;
const mockSendReaction = telegramApi.sendReaction as ReturnType<typeof vi.fn>;

interface FakeCtx {
  chat?: { id?: number; type?: string };
  msg?: { message_id?: number };
  from?: { id: number; first_name?: string };
  update: { update_id: number };
  reply: ReturnType<typeof vi.fn>;
  replyWithVoice: ReturnType<typeof vi.fn>;
  chatAction: string | undefined;
}

function makeCtx(overrides: Partial<FakeCtx> = {}): FakeCtx {
  return {
    chat: { id: 123, type: 'private' },
    msg: { message_id: 42 },
    from: { id: 123, first_name: 'Alice' },
    update: { update_id: 1 },
    reply: vi.fn().mockResolvedValue(undefined),
    replyWithVoice: vi.fn().mockResolvedValue(undefined),
    chatAction: undefined,
    ...overrides,
  };
}

describe('handleTextMessage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Default: TTS disabled to keep happy-path test simple
    mockGetUserPreferences.mockResolvedValue({ tts_enabled: false });
  });

  it('returns without side effects when chat or message context is missing', async () => {
    const ctx = makeCtx({ chat: undefined });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'hi');
    expect(ctx.reply).not.toHaveBeenCalled();
    expect(mockSendMessageToAI).not.toHaveBeenCalled();
  });

  it('saveOnly persists the message without replying', async () => {
    mockSendMessageToAI.mockResolvedValueOnce(null);
    const ctx = makeCtx();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'group chatter', { saveOnly: true });

    expect(ctx.reply).not.toHaveBeenCalled();
    expect(mockSendMessageToAI).toHaveBeenCalledWith(
      'tg:123',
      'group chatter',
      expect.objectContaining({ saveOnly: true })
    );
  });

  it('sends a single reply on happy path', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('Hello back!');
    const ctx = makeCtx();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'hi');

    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenCalledWith(
      'Hello back!',
      expect.objectContaining({
        reply_parameters: expect.objectContaining({ message_id: 42 }),
      })
    );
  });

  it('sends both a ❌ reaction AND an error reply when AI fails (H2 parity)', async () => {
    mockSendMessageToAI.mockRejectedValueOnce(new Error('AI API down'));
    const ctx = makeCtx();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'hi');

    expect(mockSendReaction).toHaveBeenCalledWith(123, 42, '❌');
    expect(ctx.reply).toHaveBeenCalledTimes(1);
    expect(ctx.reply).toHaveBeenCalledWith(expect.stringMatching(/sorry.*error processing/i));
  });

  it('still sends the error reply when the reaction itself throws', async () => {
    mockSendMessageToAI.mockRejectedValueOnce(new Error('AI crashed'));
    mockSendReaction.mockRejectedValueOnce(new Error('reaction 403'));
    const ctx = makeCtx();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'hi');

    expect(ctx.reply).toHaveBeenCalledWith(expect.stringMatching(/sorry.*error processing/i));
  });

  it('emits a voice reply when TTS is enabled', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('Spoken text');
    mockGetUserPreferences.mockResolvedValue({ tts_enabled: true });
    mockTextToSpeech.mockResolvedValueOnce(Buffer.from('OGG-AUDIO'));
    const ctx = makeCtx();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'speak');

    expect(ctx.replyWithVoice).toHaveBeenCalledTimes(1);
    expect(mockTextToSpeech).toHaveBeenCalledWith('Spoken text', 'tg:123');
  });

  it('does not throw when TTS buffer is null (logs and continues)', async () => {
    mockSendMessageToAI.mockResolvedValueOnce('Text only');
    mockGetUserPreferences.mockResolvedValue({ tts_enabled: true });
    mockTextToSpeech.mockResolvedValueOnce(null);
    const ctx = makeCtx();

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await handleTextMessage(ctx as any, 'fallback');

    expect(ctx.replyWithVoice).not.toHaveBeenCalled();
    // Text reply still happens
    expect(ctx.reply).toHaveBeenCalledWith('Text only', expect.anything());
  });
});
