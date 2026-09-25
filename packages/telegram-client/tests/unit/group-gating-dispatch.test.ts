/**
 * GROUP_GATING through the real dispatch stack (updates.ts resolveGate +
 * utils/gating.ts), with the REAL whitelist matcher.
 *
 * jid (default): a group must be listed by its own `tg:-100…` id; then every
 * member may address the bot. membership: the Bot API cannot enumerate
 * members, so every group is in scope and saved, but only a whitelisted sender
 * (`tg:<from.id>`) gets a reply — unless the group itself is listed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
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

vi.mock('../../src/logger.js', () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn(), fatal: vi.fn() },
}));

const BOT_INFO = {
  id: 1,
  is_bot: true,
  first_name: 'Assistant',
  username: 'MyBot',
} as UserFromGetMe;

const GROUP_ID = -1001234567890;
const MEMBER = 555; // whitelisted
const STRANGER = 777; // not whitelisted

function groupText(from: number, text: string, mention: boolean): Update {
  return {
    update_id: 1,
    message: {
      message_id: 100,
      date: 0,
      chat: { id: GROUP_ID, type: 'supergroup', title: 'Team chat' },
      from: { id: from, is_bot: false, first_name: 'X' },
      text,
      ...(mention ? { entities: [{ type: 'mention', offset: 0, length: 6 }] } : {}),
    },
  } as Update;
}

function privateText(from: number): Update {
  return {
    update_id: 1,
    message: {
      message_id: 100,
      date: 0,
      chat: { id: from, type: 'private', first_name: 'X' },
      from: { id: from, is_bot: false, first_name: 'X' },
      text: 'hi',
    },
  } as Update;
}

async function boot(mode: 'jid' | 'membership', whitelist: string) {
  vi.resetModules();
  vi.stubEnv('WHITELIST_PHONES', whitelist);
  vi.stubEnv('GROUP_GATING', mode);
  const api = await import('../../src/api-client.js');
  const { bot } = await import('../../src/bot.js');
  const { registerUpdateHandlers } = await import('../../src/updates.js');
  bot.botInfo = BOT_INFO;
  bot.api.config.use(async () => ({ ok: true, result: { message_id: 1 } }) as unknown as never);
  registerUpdateHandlers();
  return { bot, sendMessageToAI: api.sendMessageToAI as unknown as ReturnType<typeof vi.fn> };
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.unstubAllEnvs());

describe('GROUP_GATING=jid (default behaviour)', () => {
  it('drops a group that is not listed, even from a whitelisted user', async () => {
    const { bot, sendMessageToAI } = await boot('jid', `tg:${MEMBER}`);
    await bot.handleUpdate(groupText(MEMBER, '@MyBot hi', true));
    expect(sendMessageToAI).not.toHaveBeenCalled();
  });

  it('answers any member of a listed group', async () => {
    const { bot, sendMessageToAI } = await boot('jid', `tg:${GROUP_ID}`);
    await bot.handleUpdate(groupText(STRANGER, '@MyBot hi', true));
    expect(sendMessageToAI.mock.calls[0][2].saveOnly ?? false).toBe(false);
  });
});

describe('GROUP_GATING=membership', () => {
  it('answers a whitelisted sender in an unlisted group', async () => {
    const { bot, sendMessageToAI } = await boot('membership', `tg:${MEMBER}`);
    await bot.handleUpdate(groupText(MEMBER, '@MyBot hi', true));
    expect(sendMessageToAI).toHaveBeenCalledOnce();
    expect(sendMessageToAI.mock.calls[0][2].saveOnly ?? false).toBe(false);
  });

  it('PRIVACY: saves but never answers a non-whitelisted sender @mentioning the bot', async () => {
    const { bot, sendMessageToAI } = await boot('membership', `tg:${MEMBER}`);
    await bot.handleUpdate(groupText(STRANGER, '@MyBot hi', true));
    expect(sendMessageToAI).toHaveBeenCalledOnce();
    expect(sendMessageToAI.mock.calls[0][2].saveOnly).toBe(true);
  });

  it('saves un-addressed chatter from anyone', async () => {
    const { bot, sendMessageToAI } = await boot('membership', `tg:${MEMBER}`);
    await bot.handleUpdate(groupText(STRANGER, 'chatter', false));
    expect(sendMessageToAI.mock.calls[0][2].saveOnly).toBe(true);
  });

  it('a listed group still answers every member', async () => {
    const { bot, sendMessageToAI } = await boot('membership', `tg:${GROUP_ID}`);
    await bot.handleUpdate(groupText(STRANGER, '@MyBot hi', true));
    expect(sendMessageToAI.mock.calls[0][2].saveOnly ?? false).toBe(false);
  });

  it('private chats are still gated on the chat id', async () => {
    const { bot, sendMessageToAI } = await boot('membership', `tg:${MEMBER}`);
    await bot.handleUpdate(privateText(STRANGER));
    expect(sendMessageToAI).not.toHaveBeenCalled();
    await bot.handleUpdate(privateText(MEMBER));
    expect(sendMessageToAI).toHaveBeenCalledOnce();
  });
});
