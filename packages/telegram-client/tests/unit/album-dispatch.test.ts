/**
 * Album photos (shared media_group_id) reach the AI API as one request.
 * Real registerUpdateHandlers() + middleware, driven by bot.handleUpdate.
 */
import { describe, it, expect, vi } from 'vitest';
import type { Update, UserFromGetMe } from 'grammy/types';

vi.mock('../../src/config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/config.js')>();
  return {
    config: { ...actual.config, album: { quietMs: 30, maxWaitMs: 1000, maxItems: 10 } },
  };
});

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(async () => 'nice photos'),
  getUserPreferences: vi.fn(async () => ({ tts_enabled: false })),
  textToSpeech: vi.fn(async () => null),
}));

vi.mock('../../src/handlers/photo.js', () => ({
  extractPhotoData: vi.fn(async (ctx: { msg: { message_id: number } }) => ({
    kind: 'ok',
    data: `img-${ctx.msg.message_id}`,
    mimetype: 'image/jpeg',
  })),
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

const USER_ID = 555;

const GROUP_ID = -100777;

function albumPhoto(messageId: number, caption?: string, group = false): Update {
  const mention = caption?.indexOf('@MyBot') ?? -1;
  return {
    update_id: messageId,
    message: {
      message_id: messageId,
      date: 0,
      media_group_id: group ? 'album-g' : 'album-1',
      chat: group
        ? { id: GROUP_ID, type: 'supergroup', title: 'G' }
        : { id: USER_ID, type: 'private', first_name: 'Ana' },
      from: { id: USER_ID, is_bot: false, first_name: 'Ana' },
      photo: [{ file_id: `f${messageId}`, file_unique_id: `u${messageId}`, width: 1, height: 1 }],
      ...(caption ? { caption } : {}),
      ...(mention >= 0
        ? { caption_entities: [{ type: 'mention', offset: mention, length: 6 }] }
        : {}),
    },
  } as Update;
}

describe('Telegram albums', () => {
  it('sends the photos of an album in one request with its caption', async () => {
    const api = await import('../../src/api-client.js');
    const { bot } = await import('../../src/bot.js');
    const { registerUpdateHandlers } = await import('../../src/updates.js');
    bot.botInfo = { id: 1, is_bot: true, first_name: 'B', username: 'MyBot' } as UserFromGetMe;
    bot.api.config.use(async () => ({ ok: true, result: { message_id: 1 } }) as unknown as never);
    registerUpdateHandlers();

    await Promise.all([
      bot.handleUpdate(albumPhoto(11, 'our trip')),
      bot.handleUpdate(albumPhoto(12)),
      bot.handleUpdate(albumPhoto(13)),
    ]);

    const send = api.sendMessageToAI as unknown as ReturnType<typeof vi.fn>;
    expect(send).toHaveBeenCalledTimes(1);
    const [, text, options] = send.mock.calls[0];
    expect(text).toBe('our trip');
    expect(options.images.map((i: { data: string }) => i.data)).toEqual([
      'img-11',
      'img-12',
      'img-13',
    ]);
  });

  async function setup() {
    vi.clearAllMocks();
    const api = await import('../../src/api-client.js');
    const photo = await import('../../src/handlers/photo.js');
    const { bot } = await import('../../src/bot.js');
    return {
      bot,
      send: api.sendMessageToAI as unknown as ReturnType<typeof vi.fn>,
      extract: photo.extractPhotoData as unknown as ReturnType<typeof vi.fn>,
    };
  }

  it('answers a group album when only its caption mentions the bot', async () => {
    const { bot, send } = await setup();

    await Promise.all([
      bot.handleUpdate(albumPhoto(21, '@MyBot compare these', true)),
      bot.handleUpdate(albumPhoto(22, undefined, true)),
      bot.handleUpdate(albumPhoto(23, undefined, true)),
    ]);

    const answered = send.mock.calls.filter(([, , o]) => !o?.saveOnly);
    expect(answered).toHaveLength(1);
    const [, text, options] = answered[0];
    expect(text).toBe('compare these');
    expect(options.images).toHaveLength(3);
    expect(options.isGroupAdmin).toBe(true);
  });

  it('saves an un-addressed group album as history only, without downloading', async () => {
    const { bot, send, extract } = await setup();

    await Promise.all([
      bot.handleUpdate({ ...albumPhoto(31, 'look', true), update_id: 31 }),
      bot.handleUpdate(albumPhoto(32, undefined, true)),
    ]);

    expect(extract).not.toHaveBeenCalled();
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls.every(([, , o]) => o?.saveOnly === true)).toBe(true);
  });
});
