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
import { sendImage, sendReaction, sendText } from '../../src/services/telegram-api.js';

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

describe('sendText', () => {
  let sendMessageSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.restoreAllMocks();
    sendMessageSpy = vi.spyOn(bot.api, 'sendMessage');
  });

  it('sends WhatsApp markup as Telegram HTML', async () => {
    sendMessageSpy.mockResolvedValueOnce({ message_id: 7 } as never);

    await expect(sendText(1, 'a *b* <c>')).resolves.toBe(7);
    expect(sendMessageSpy).toHaveBeenCalledWith(1, 'a <b>b</b> &lt;c&gt;', {
      parse_mode: 'HTML',
    });
  });

  it("retries once as plain text on can't parse entities", async () => {
    sendMessageSpy
      .mockRejectedValueOnce(
        new GrammyError(
          'x',
          { ok: false, error_code: 400, description: "Bad Request: can't parse entities" },
          'sendMessage',
          {}
        )
      )
      .mockResolvedValueOnce({ message_id: 8 } as never);

    await expect(sendText(1, 'a *b*')).resolves.toBe(8);
    expect(sendMessageSpy).toHaveBeenLastCalledWith(1, 'a *b*', {});
  });

  it('does not retry other errors', async () => {
    sendMessageSpy.mockRejectedValueOnce(new Error('network'));
    await expect(sendText(1, 'x')).rejects.toThrow('network');
    expect(sendMessageSpy).toHaveBeenCalledTimes(1);
  });

  it('splits at 4096 chars, threads only the first piece, returns its id', async () => {
    let id = 100;
    sendMessageSpy.mockImplementation(async () => ({ message_id: id++ }) as never);

    const result = await sendText(1, 'word '.repeat(2000), 55);

    expect(result).toBe(100);
    expect(sendMessageSpy.mock.calls.length).toBeGreaterThan(1);
    for (const call of sendMessageSpy.mock.calls) {
      expect((call[1] as string).length).toBeLessThanOrEqual(4096);
    }
    expect(sendMessageSpy.mock.calls[0][2]).toMatchObject({
      reply_parameters: { message_id: 55 },
    });
    expect(sendMessageSpy.mock.calls[1][2]).not.toHaveProperty('reply_parameters');
  });
});

describe('sendImage', () => {
  let sendPhotoSpy: ReturnType<typeof vi.spyOn>;
  const png = Buffer.from('png');

  beforeEach(() => {
    vi.restoreAllMocks();
    sendPhotoSpy = vi.spyOn(bot.api, 'sendPhoto');
  });

  it('sends the caption as Telegram HTML and quotes the given message', async () => {
    sendPhotoSpy.mockResolvedValueOnce({ message_id: 9 } as never);

    await expect(sendImage(1, png, 'image.png', 'Fire *map*', 42)).resolves.toBe(9);
    expect(sendPhotoSpy.mock.calls[0][2]).toEqual({
      caption: 'Fire <b>map</b>',
      parse_mode: 'HTML',
      reply_parameters: { message_id: 42, allow_sending_without_reply: true },
    });
  });

  it('sends no caption or reply parameters when none are given', async () => {
    sendPhotoSpy.mockResolvedValueOnce({ message_id: 9 } as never);

    await sendImage(1, png, 'image.png');
    expect(sendPhotoSpy.mock.calls[0][2]).toEqual({});
  });

  it("resends with the plain caption on can't parse entities", async () => {
    sendPhotoSpy
      .mockRejectedValueOnce(
        new GrammyError(
          'x',
          { ok: false, error_code: 400, description: "Bad Request: can't parse entities" },
          'sendPhoto',
          {}
        )
      )
      .mockResolvedValueOnce({ message_id: 10 } as never);

    await expect(sendImage(1, png, 'image.png', 'a *b*')).resolves.toBe(10);
    expect(sendPhotoSpy.mock.calls[1][2]).toEqual({ caption: 'a *b*' });
  });

  it('uses the plain caption when the HTML would pass 1024 characters', async () => {
    sendPhotoSpy.mockResolvedValueOnce({ message_id: 11 } as never);
    const caption = '*b* '.repeat(250); // 1000 chars raw, longer as HTML

    await sendImage(1, png, 'image.png', caption);
    expect(sendPhotoSpy.mock.calls[0][2]).toEqual({ caption });
  });
});
