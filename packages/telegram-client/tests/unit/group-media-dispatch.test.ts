/**
 * Group documents and voice notes: the bot mention is stripped from the
 * caption and the sender's admin status is sent, like text and photos.
 * Real registerUpdateHandlers() + middleware, driven by bot.handleUpdate.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';

// The whitelist comes from the AI API now; serve WHITELIST_PHONES from the env.
vi.mock('../../src/services/live-whitelist.js', async () =>
  (await import('../helpers/live-whitelist-env.js')).envWhitelistMock()
);
import type { Update, UserFromGetMe } from 'grammy/types';

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(async () => 'ok'),
  getUserPreferences: vi.fn(async () => ({ tts_enabled: false })),
  textToSpeech: vi.fn(async () => null),
}));

vi.mock('../../src/handlers/document.js', () => ({
  extractDocumentData: vi.fn(async () => ({
    kind: 'ok',
    data: 'pdf',
    mimetype: 'application/pdf',
    filename: 'a.pdf',
  })),
}));

vi.mock('../../src/handlers/voice.js', () => ({
  extractAndTranscribeVoice: vi.fn(async () => ({ kind: 'ok', transcription: 'stop it' })),
}));

vi.mock('../../src/services/telegram-api.js', () => ({
  sendReaction: vi.fn(async () => undefined),
  sendChatAction: vi.fn(async () => undefined),
  isParseEntitiesError: () => false,
}));

vi.mock('../../src/utils/whitelist.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/utils/whitelist.js')>();
  return { ...actual, isWhitelisted: () => true };
});

vi.mock('../../src/services/group-admin.js', () => ({
  isSenderGroupAdmin: vi.fn(async () => true),
}));

vi.mock('../../src/logger.js', () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn(), fatal: vi.fn() },
}));

const GROUP = { id: -100555, type: 'supergroup', title: 'G' } as const;
const FROM = { id: 42, is_bot: false, first_name: 'Ana' };

function groupMessage(id: number, extra: Record<string, unknown>): Update {
  return {
    update_id: id,
    message: { message_id: id, date: 0, chat: GROUP, from: FROM, ...extra },
  } as unknown as Update;
}

async function sendMock() {
  const api = await import('../../src/api-client.js');
  return api.sendMessageToAI as unknown as ReturnType<typeof vi.fn>;
}

describe('group documents and voice notes', () => {
  beforeAll(async () => {
    const { bot } = await import('../../src/bot.js');
    const { registerUpdateHandlers } = await import('../../src/updates.js');
    bot.botInfo = { id: 1, is_bot: true, first_name: 'B', username: 'MyBot' } as UserFromGetMe;
    bot.api.config.use(async () => ({ ok: true, result: { message_id: 1 } }) as unknown as never);
    registerUpdateHandlers();
  });

  beforeEach(() => vi.clearAllMocks());

  it('strips the mention from a document caption and sends the admin flag', async () => {
    const { bot } = await import('../../src/bot.js');
    await bot.handleUpdate(
      groupMessage(1, {
        document: { file_id: 'f', file_unique_id: 'u', file_name: 'a.pdf' },
        caption: '@MyBot turn off broadcasts here',
        caption_entities: [{ type: 'mention', offset: 0, length: 6 }],
      })
    );

    const send = await sendMock();
    expect(send).toHaveBeenCalledTimes(1);
    const [, text, options] = send.mock.calls[0];
    expect(text).toBe('turn off broadcasts here');
    expect(options.isGroupAdmin).toBe(true);
  });

  it('sends the admin flag for an addressed group voice note', async () => {
    const { bot } = await import('../../src/bot.js');
    await bot.handleUpdate(
      groupMessage(2, {
        voice: { file_id: 'v', file_unique_id: 'vu', duration: 1 },
        reply_to_message: {
          message_id: 99,
          date: 0,
          chat: GROUP,
          from: { id: 1, is_bot: true, first_name: 'B', username: 'MyBot' },
          text: 'hi',
        },
      })
    );

    const send = await sendMock();
    expect(send).toHaveBeenCalledTimes(1);
    expect(send.mock.calls[0][2].isGroupAdmin).toBe(true);
  });
});
