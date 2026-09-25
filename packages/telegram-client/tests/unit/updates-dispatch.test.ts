/**
 * End-to-end dispatch tests: real `registerUpdateHandlers()`, real middleware
 * stack, driven by `bot.handleUpdate`.
 *
 * Covers the two group-path behaviours that other suites cannot see (they
 * leave `botInfo` unset, which makes `isAddressed()` always false):
 *   - `isGroupAdmin` must be resolved and forwarded for group commands — the
 *     AI API fails closed, so an omitted flag locks admins out, and before the
 *     fail-closed change it let any member run `/clean all`;
 *   - `/settings@Bot` (a single `bot_command` entity) must count as addressed.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Update, UserFromGetMe } from 'grammy/types';

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(async () => 'reply'),
  getUserPreferences: vi.fn(async () => ({ tts_enabled: false })),
  textToSpeech: vi.fn(async () => null),
}));

vi.mock('../../src/services/telegram-api.js', () => ({
  sendReaction: vi.fn(async () => undefined),
  sendChatAction: vi.fn(async () => undefined),
  isParseEntitiesError: () => false,
}));

vi.mock('../../src/services/group-admin.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/services/group-admin.js')>();
  return { ...actual, isSenderGroupAdmin: vi.fn(async () => false) };
});

// A developer's root .env may set WHITELIST_PHONES; the gate is covered by
// whitelist.test.ts, so admit every chat here.
vi.mock('../../src/utils/whitelist.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/utils/whitelist.js')>();
  return { ...actual, isWhitelisted: () => true };
});

vi.mock('../../src/logger.js', () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn(), fatal: vi.fn() },
}));

const BOT_INFO: UserFromGetMe = {
  id: 1,
  is_bot: true,
  first_name: 'Assistant',
  username: 'MyBot',
  can_join_groups: true,
  can_read_all_group_messages: true,
  supports_inline_queries: false,
  can_connect_to_business: false,
  has_main_web_app: false,
} as UserFromGetMe;

const GROUP_ID = -1001234567890;
const USER_ID = 555;

interface Entity {
  type: string;
  offset: number;
  length: number;
}

function groupText(text: string, entities: Entity[] = [], updateId = 1): Update {
  return {
    update_id: updateId,
    message: {
      message_id: 100,
      date: 0,
      chat: { id: GROUP_ID, type: 'supergroup', title: 'Team chat' },
      from: { id: USER_ID, is_bot: false, first_name: 'Ana' },
      text,
      ...(entities.length ? { entities } : {}),
    },
  } as Update;
}

function privateText(text: string, updateId = 1): Update {
  return {
    update_id: updateId,
    message: {
      message_id: 100,
      date: 0,
      chat: { id: USER_ID, type: 'private', first_name: 'Ana' },
      from: { id: USER_ID, is_bot: false, first_name: 'Ana' },
      text,
    },
  } as Update;
}

const mention = (username: string): Entity[] => [
  { type: 'mention', offset: 0, length: username.length + 1 },
];

const command = (text: string): Entity[] => [
  { type: 'bot_command', offset: 0, length: text.split(' ')[0].length },
];

async function freshDispatch() {
  vi.resetModules();
  const api = await import('../../src/api-client.js');
  const groupAdmin = await import('../../src/services/group-admin.js');
  const { bot } = await import('../../src/bot.js');
  const { registerUpdateHandlers } = await import('../../src/updates.js');
  bot.botInfo = BOT_INFO;
  // Never hit the network: every outbound Bot API call (ctx.reply,
  // sendChatAction from auto-chat-action) resolves with a stub message.
  bot.api.config.use(async () => ({ ok: true, result: { message_id: 1 } }) as unknown as never);
  registerUpdateHandlers();
  return {
    bot,
    sendMessageToAI: api.sendMessageToAI as unknown as ReturnType<typeof vi.fn>,
    isSenderGroupAdmin: groupAdmin.isSenderGroupAdmin as unknown as ReturnType<typeof vi.fn>,
  };
}

function expectAnswered(options: { saveOnly?: boolean }) {
  expect(options.saveOnly ?? false).toBe(false);
}

function expectSavedOnly(options: { saveOnly?: boolean }) {
  expect(options.saveOnly).toBe(true);
}

describe('group dispatch', () => {
  beforeEach(() => vi.clearAllMocks());

  it('answers an @mention', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    await bot.handleUpdate(groupText('@MyBot good morning', mention('MyBot')));
    expect(sendMessageToAI).toHaveBeenCalledTimes(1);
    expectAnswered(sendMessageToAI.mock.calls[0][2]);
    expect(sendMessageToAI.mock.calls[0][1]).toBe('good morning');
  });

  it('answers a lowercase mention', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    await bot.handleUpdate(groupText('@mybot good morning', mention('mybot')));
    expectAnswered(sendMessageToAI.mock.calls[0][2]);
  });

  it('saves an un-addressed message without answering', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    await bot.handleUpdate(groupText('chatter between other people'));
    expectSavedOnly(sendMessageToAI.mock.calls[0][2]);
  });
});

describe('group admin gating', () => {
  beforeEach(() => vi.clearAllMocks());

  it('forwards isGroupAdmin=false for a non-admin command', async () => {
    const { bot, sendMessageToAI, isSenderGroupAdmin } = await freshDispatch();
    isSenderGroupAdmin.mockResolvedValue(false);
    await bot.handleUpdate(groupText('@MyBot /clean all', mention('MyBot')));
    expect(sendMessageToAI.mock.calls[0][2]).toMatchObject({ isGroupAdmin: false });
  });

  it('forwards isGroupAdmin=true for an admin', async () => {
    const { bot, sendMessageToAI, isSenderGroupAdmin } = await freshDispatch();
    isSenderGroupAdmin.mockResolvedValue(true);
    await bot.handleUpdate(groupText('@MyBot /clean all', mention('MyBot')));
    expect(sendMessageToAI.mock.calls[0][2]).toMatchObject({ isGroupAdmin: true });
  });

  it('resolves admin status for the /cmd@Bot form', async () => {
    const { bot, sendMessageToAI, isSenderGroupAdmin } = await freshDispatch();
    isSenderGroupAdmin.mockResolvedValue(true);
    await bot.handleUpdate(groupText('/clean@MyBot all', command('/clean@MyBot all')));
    expect(isSenderGroupAdmin).toHaveBeenCalledTimes(1);
    expect(sendMessageToAI.mock.calls[0][2]).toMatchObject({ isGroupAdmin: true });
  });

  it('resolves admin status for an addressed non-command message', async () => {
    // Agent tools that change a group's settings ("@MyBot stop the
    // announcements") need it too, and the AI API fails closed without it.
    const { bot, sendMessageToAI, isSenderGroupAdmin } = await freshDispatch();
    isSenderGroupAdmin.mockResolvedValue(true);
    await bot.handleUpdate(groupText('@MyBot stop the announcements', mention('MyBot')));
    expect(isSenderGroupAdmin).toHaveBeenCalledTimes(1);
    expect(sendMessageToAI.mock.calls[0][2]).toMatchObject({ isGroupAdmin: true });
  });

  it('does NOT look up admin status in private chats', async () => {
    const { bot, isSenderGroupAdmin } = await freshDispatch();
    await bot.handleUpdate(privateText('/clean all'));
    expect(isSenderGroupAdmin).not.toHaveBeenCalled();
  });

  it('does NOT look up admin status for an un-addressed group message', async () => {
    const { bot, isSenderGroupAdmin } = await freshDispatch();
    await bot.handleUpdate(groupText('/clean all someone said'));
    expect(isSenderGroupAdmin).not.toHaveBeenCalled();
  });
});

describe('command forms Telegram actually delivers', () => {
  beforeEach(() => vi.clearAllMocks());

  it('answers /settings@MyBot in a group', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    await bot.handleUpdate(groupText('/settings@MyBot', command('/settings@MyBot')));
    expectAnswered(sendMessageToAI.mock.calls[0][2]);
  });

  it('ignores a command aimed at another bot', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    await bot.handleUpdate(groupText('/settings@OtherBot', command('/settings@OtherBot')));
    expectSavedOnly(sendMessageToAI.mock.calls[0][2]);
  });

  it('answers a bare /settings in a group', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    await bot.handleUpdate(groupText('/settings', command('/settings')));
    expectAnswered(sendMessageToAI.mock.calls[0][2]);
  });
});

describe('phone-link dispatch', () => {
  beforeEach(() => vi.clearAllMocks());

  it('/linkphone is handled by the client, never forwarded to the AI API', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    const update = privateText('/linkphone');
    (update.message as { entities?: Entity[] }).entities = command('/linkphone');
    await bot.handleUpdate(update);
    expect(sendMessageToAI).not.toHaveBeenCalled();
  });

  it('/link with a code still reaches the AI API (code flow unchanged)', async () => {
    const { bot, sendMessageToAI } = await freshDispatch();
    const update = privateText('/link 123456');
    (update.message as { entities?: Entity[] }).entities = command('/link 123456');
    await bot.handleUpdate(update);
    expect(sendMessageToAI).toHaveBeenCalledTimes(1);
    expect(sendMessageToAI.mock.calls[0][1]).toBe('/link 123456');
  });
});
