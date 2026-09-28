/**
 * Unit tests for services/group-admin.ts.
 *
 * The AI API fails closed on group admin commands (anything but an explicit
 * `true` is refused), so the lookup must always yield a real boolean and must
 * report "not an admin" whenever getChatMember cannot answer.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../src/logger.js', () => ({
  logger: { error: vi.fn(), warn: vi.fn(), info: vi.fn(), debug: vi.fn() },
}));

import { bot } from '../../src/bot.js';
import { ADMIN_LOOKUP_TIMEOUT_MS, isSenderGroupAdmin } from '../../src/services/group-admin.js';
import type { TelegramContext } from '../../src/bot.js';

function ctx(chatId?: number, userId?: number, senderChatId?: number): TelegramContext {
  return {
    chat: chatId === undefined ? undefined : { id: chatId, type: 'supergroup' },
    from: userId === undefined ? undefined : { id: userId, is_bot: false, first_name: 'A' },
    msg:
      senderChatId === undefined
        ? {}
        : {
            sender_chat: {
              id: senderChatId,
              type: senderChatId === chatId ? 'supergroup' : 'channel',
            },
          },
  } as unknown as TelegramContext;
}

const GROUP_ANONYMOUS_BOT = 1087968824;

describe('isSenderGroupAdmin', () => {
  let spy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    spy = vi.spyOn(bot.api, 'getChatMember');
  });

  it.each(['creator', 'administrator'])('returns true for %s', async (status) => {
    spy.mockResolvedValue({ status } as never);
    await expect(isSenderGroupAdmin(ctx(-100, 5))).resolves.toBe(true);
    expect(spy).toHaveBeenCalledWith(-100, 5);
  });

  it.each(['member', 'restricted', 'left', 'kicked'])('returns false for %s', async (status) => {
    spy.mockResolvedValue({ status } as never);
    await expect(isSenderGroupAdmin(ctx(-100, 5))).resolves.toBe(false);
  });

  it('fails closed when getChatMember throws', async () => {
    spy.mockRejectedValue(new Error('network down'));
    await expect(isSenderGroupAdmin(ctx(-100, 5))).resolves.toBe(false);
  });

  it('returns false without a Bot API call when chat or sender is missing', async () => {
    await expect(isSenderGroupAdmin(ctx(undefined, 5))).resolves.toBe(false);
    await expect(isSenderGroupAdmin(ctx(-100, undefined))).resolves.toBe(false);
    expect(spy).not.toHaveBeenCalled();
  });

  it('fails closed (not admin) when getChatMember hangs past the timeout', async () => {
    vi.useFakeTimers();
    try {
      spy.mockReturnValue(new Promise(() => {}) as never);
      const result = isSenderGroupAdmin(ctx(-100, 5));
      await vi.advanceTimersByTimeAsync(ADMIN_LOOKUP_TIMEOUT_MS);
      await expect(result).resolves.toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('treats an anonymous admin (posting as the group itself) as admin, without an API call', async () => {
    await expect(isSenderGroupAdmin(ctx(-100, GROUP_ANONYMOUS_BOT, -100))).resolves.toBe(true);
    expect(spy).not.toHaveBeenCalled();
  });

  it('does NOT treat a post as another chat (linked channel / own channel) as admin', async () => {
    spy.mockResolvedValue({ status: 'member' } as never);
    await expect(isSenderGroupAdmin(ctx(-100, 777000, -200))).resolves.toBe(false);
  });
});
