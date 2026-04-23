/**
 * Unit tests for services/telegram-api.ts sendReaction error handling.
 *
 * The catch block was narrowed in the review follow-up: it now swallows
 * only 400 REACTION_INVALID from GrammyError, and re-throws everything else
 * (401, 403, 429, network errors, plain Errors) so they surface through the
 * bot.catch boundary.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { GrammyError } from 'grammy';
import { bot } from '../../src/bot.js';
import { sendReaction } from '../../src/services/telegram-api.js';

function makeGrammyError(errorCode: number, description: string): GrammyError {
  // GrammyError's constructor signature: (message, errorObj, method, payload).
  // We use its own shape so `error_code` and `description` are populated.
  return new GrammyError(
    `Telegram server error`,
    { ok: false, error_code: errorCode, description },
    'setMessageReaction',
    {}
  );
}

describe('sendReaction error handling', () => {
  let setReactionSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    setReactionSpy = vi.spyOn(bot.api, 'setMessageReaction');
  });

  it('swallows 400 REACTION_INVALID errors', async () => {
    setReactionSpy.mockRejectedValueOnce(makeGrammyError(400, 'Bad Request: REACTION_INVALID'));

    await expect(sendReaction(1, 2, '❌')).resolves.toBeUndefined();
  });

  it('re-throws GrammyError with error_code 403 (bot kicked from chat)', async () => {
    const err = makeGrammyError(403, 'Forbidden: bot was kicked from the group chat');
    setReactionSpy.mockRejectedValueOnce(err);

    await expect(sendReaction(1, 2, '❌')).rejects.toBe(err);
  });

  it('re-throws GrammyError with error_code 401 (revoked token)', async () => {
    const err = makeGrammyError(401, 'Unauthorized');
    setReactionSpy.mockRejectedValueOnce(err);

    await expect(sendReaction(1, 2, '❌')).rejects.toBe(err);
  });

  it('re-throws GrammyError with error_code 429 (rate limit)', async () => {
    const err = makeGrammyError(429, 'Too Many Requests: retry after 5');
    setReactionSpy.mockRejectedValueOnce(err);

    await expect(sendReaction(1, 2, '❌')).rejects.toBe(err);
  });

  it('re-throws GrammyError with other 400 descriptions (not REACTION_INVALID)', async () => {
    const err = makeGrammyError(400, 'Bad Request: message to react not found');
    setReactionSpy.mockRejectedValueOnce(err);

    await expect(sendReaction(1, 2, '❌')).rejects.toBe(err);
  });

  it('re-throws plain Error objects', async () => {
    const err = new Error('network connection refused');
    setReactionSpy.mockRejectedValueOnce(err);

    await expect(sendReaction(1, 2, '❌')).rejects.toBe(err);
  });

  it('resolves successfully on happy path', async () => {
    setReactionSpy.mockResolvedValueOnce(true as unknown as never);

    await expect(sendReaction(1, 2, '❌')).resolves.toBeUndefined();
    expect(setReactionSpy).toHaveBeenCalledWith(1, 2, [{ type: 'emoji', emoji: '👎' }]);
  });
});
