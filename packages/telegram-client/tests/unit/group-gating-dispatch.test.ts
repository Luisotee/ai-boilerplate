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

// The whitelist comes from the AI API now; serve WHITELIST_PHONES from the env.
vi.mock('../../src/services/live-whitelist.js', async () =>
  (await import('../helpers/live-whitelist-env.js')).envWhitelistMock()
);
import type { Update, UserFromGetMe } from 'grammy/types';

vi.mock('../../src/api-client.js', () => ({
  sendMessageToAI: vi.fn(async () => 'reply'),
  getUserPreferences: vi.fn(async () => ({ tts_enabled: false })),
  textToSpeech: vi.fn(async () => null),
  editMessage: vi.fn(async () => 1),
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
  return {
    bot,
    sendMessageToAI: api.sendMessageToAI as unknown as ReturnType<typeof vi.fn>,
    editMessage: api.editMessage as unknown as ReturnType<typeof vi.fn>,
  };
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

describe('"@bot, then the question" window', () => {
  const saveOnly = (fn: ReturnType<typeof vi.fn>, i: number) =>
    fn.mock.calls[i][2].saveOnly === true;

  it("answers the mentioning sender's next message once, and nobody else's", async () => {
    const { bot, sendMessageToAI } = await boot('jid', `tg:${GROUP_ID}`);
    await bot.handleUpdate(groupText(MEMBER, '@MyBot', true));
    await bot.handleUpdate(groupText(STRANGER, 'unrelated', false));
    await bot.handleUpdate(groupText(MEMBER, 'the question', false));
    await bot.handleUpdate(groupText(MEMBER, 'thanks', false));

    expect(saveOnly(sendMessageToAI, 0)).toBe(false);
    expect(saveOnly(sendMessageToAI, 1)).toBe(true);
    expect(saveOnly(sendMessageToAI, 2)).toBe(false);
    expect(saveOnly(sendMessageToAI, 3)).toBe(true);
  });

  it('a typed @first_name with no other mention addresses the bot', async () => {
    const { bot, sendMessageToAI } = await boot('jid', `tg:${GROUP_ID}`);
    // (The entity-tagged form is covered in mention.test.ts.)
    await bot.handleUpdate(groupText(MEMBER, '@Assistant oi', false));
    expect(saveOnly(sendMessageToAI, 0)).toBe(false);
  });

  it('PRIVACY: a non-whitelisted sender stays save-only inside the window (membership)', async () => {
    const { bot, sendMessageToAI } = await boot('membership', `tg:${MEMBER}`);
    await bot.handleUpdate(groupText(STRANGER, '@MyBot', true));
    await bot.handleUpdate(groupText(STRANGER, 'the question', false));
    expect(saveOnly(sendMessageToAI, 0)).toBe(true);
    expect(saveOnly(sendMessageToAI, 1)).toBe(true);
  });
});

describe('edited_message', () => {
  function groupEdit(from: number, text: string): Update {
    return {
      update_id: 2,
      edited_message: {
        message_id: 100,
        date: 0,
        edit_date: 1,
        chat: { id: GROUP_ID, type: 'supergroup', title: 'Team chat' },
        from: { id: from, is_bot: false, first_name: 'X' },
        text,
      },
    } as Update;
  }

  it('forwards an edit with the chat, message id and editor, and never replies', async () => {
    const { bot, sendMessageToAI, editMessage } = await boot('jid', `tg:${GROUP_ID}`);
    await bot.handleUpdate(groupEdit(MEMBER, 'fixed'));
    expect(editMessage).toHaveBeenCalledWith(
      { whatsappJid: `tg:${GROUP_ID}`, messageId: '100', senderJid: `tg:${MEMBER}` },
      'fixed'
    );
    expect(sendMessageToAI).not.toHaveBeenCalled();
  });

  it('drops an edit in a chat outside the whitelist', async () => {
    const { bot, editMessage } = await boot('jid', `tg:${MEMBER}`);
    await bot.handleUpdate(groupEdit(MEMBER, 'fixed'));
    expect(editMessage).not.toHaveBeenCalled();
  });

  it('an edit neither opens nor consumes the mention window', async () => {
    const { bot, sendMessageToAI } = await boot('jid', `tg:${GROUP_ID}`);
    await bot.handleUpdate(groupText(MEMBER, '@MyBot', true));
    await bot.handleUpdate(groupEdit(MEMBER, '@MyBot now with a question'));
    await bot.handleUpdate(groupText(MEMBER, 'the question', false));
    expect(sendMessageToAI.mock.calls[1][2].saveOnly ?? false).toBe(false);
  });
});

describe('live whitelist unavailable', () => {
  it('FAILS CLOSED: drops private and group updates, and edits, without a reply', async () => {
    vi.stubEnv('WHITELIST_UNAVAILABLE', '1');
    const { bot, sendMessageToAI, editMessage } = await boot('membership', '');
    await bot.handleUpdate(privateText(MEMBER));
    await bot.handleUpdate(groupText(MEMBER, '@MyBot hi', true));
    expect(sendMessageToAI).not.toHaveBeenCalled();
    expect(editMessage).not.toHaveBeenCalled();
  });
});
