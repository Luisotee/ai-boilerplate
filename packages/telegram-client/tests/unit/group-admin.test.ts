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
import { isSenderGroupAdmin } from '../../src/services/group-admin.js';
import type { TelegramContext } from '../../src/bot.js';

function ctx(chatId?: number, userId?: number): TelegramContext {
  return {
    chat: chatId === undefined ? undefined : { id: chatId, type: 'supergroup' },
    from: userId === undefined ? undefined : { id: userId, is_bot: false, first_name: 'A' },
  } as unknown as TelegramContext;
}

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
});
