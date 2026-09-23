/**
 * telegramApi.isChatMember — maps getChatMember's ChatMember union to a boolean.
 * A restricted user counts only while `is_member` is true (a restricted user who
 * left keeps the status); left/kicked never count; errors propagate.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const getChatMember = vi.fn();

vi.mock('../../src/bot.js', () => ({
  bot: { api: { getChatMember: (...args: unknown[]) => getChatMember(...args) } },
}));

import { isChatMember } from '../../src/services/telegram-api.js';

beforeEach(() => getChatMember.mockReset());

describe('isChatMember', () => {
  it.each([
    [{ status: 'creator' }, true],
    [{ status: 'administrator' }, true],
    [{ status: 'member' }, true],
    [{ status: 'restricted', is_member: true }, true],
    [{ status: 'restricted', is_member: false }, false],
    [{ status: 'left' }, false],
    [{ status: 'kicked' }, false],
  ])('%j -> %s', async (member, expected) => {
    getChatMember.mockResolvedValueOnce(member);
    expect(await isChatMember(-1001, 5)).toBe(expected);
    expect(getChatMember).toHaveBeenCalledWith(-1001, 5);
  });

  it('propagates a lookup error', async () => {
    getChatMember.mockRejectedValueOnce(new Error('network'));
    await expect(isChatMember(-1001, 5)).rejects.toThrow('network');
  });
});
