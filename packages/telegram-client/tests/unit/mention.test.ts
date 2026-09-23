import { describe, it, expect } from 'vitest';
import type { Message } from 'grammy/types';
import { isAddressedToBot, stripBotMention } from '../../src/utils/mention.js';

const bot = { id: 42, username: 'my_bot' };

function makeTextMessage(text: string, extra: Partial<Message> = {}): Message {
  return {
    message_id: 1,
    date: Math.floor(Date.now() / 1000),
    chat: { id: -100, type: 'supergroup', title: 'Test Group' },
    from: { id: 7, is_bot: false, first_name: 'Alice' },
    text,
    ...extra,
  } as Message;
}

describe('isAddressedToBot', () => {
  it('returns false for plain group chatter', () => {
    const msg = makeTextMessage('hello everyone');
    expect(isAddressedToBot(msg, bot)).toBe(false);
  });

  it('matches a @bot mention by entity offset/length', () => {
    const msg = makeTextMessage('hey @my_bot what is up', {
      entities: [{ type: 'mention', offset: 4, length: 7 }],
    });
    expect(isAddressedToBot(msg, bot)).toBe(true);
  });

  it('does not match an @other mention', () => {
    const msg = makeTextMessage('hey @other_bot what is up', {
      entities: [{ type: 'mention', offset: 4, length: 10 }],
    });
    expect(isAddressedToBot(msg, bot)).toBe(false);
  });

  it('matches a text_mention via user.id', () => {
    const msg = makeTextMessage('hey buddy', {
      entities: [
        {
          type: 'text_mention',
          offset: 4,
          length: 5,
          user: { id: 42, is_bot: true, first_name: 'My Bot' },
        },
      ],
    });
    expect(isAddressedToBot(msg, bot)).toBe(true);
  });

  it('ignores text_mention for other users', () => {
    const msg = makeTextMessage('hey someone', {
      entities: [
        {
          type: 'text_mention',
          offset: 4,
          length: 7,
          user: { id: 99, is_bot: false, first_name: 'Other' },
        },
      ],
    });
    expect(isAddressedToBot(msg, bot)).toBe(false);
  });

  it('matches a reply to the bot', () => {
    const msg = makeTextMessage('no wait', {
      reply_to_message: {
        message_id: 0,
        date: 0,
        chat: { id: -100, type: 'supergroup', title: 'Test Group' },
        from: { id: 42, is_bot: true, first_name: 'My Bot', username: 'my_bot' },
        text: 'previous bot message',
      } as Message,
    });
    expect(isAddressedToBot(msg, bot)).toBe(true);
  });
});

describe('stripBotMention', () => {
  it('removes a leading @bot mention', () => {
    expect(stripBotMention('@my_bot hello there', bot)).toBe('hello there');
  });

  it('leaves non-leading mentions alone', () => {
    expect(stripBotMention('hello @my_bot there', bot)).toBe('hello @my_bot there');
  });

  it('is case-insensitive', () => {
    expect(stripBotMention('@My_Bot hi', bot)).toBe('hi');
  });

  it('returns the original string when username does not match', () => {
    expect(stripBotMention('@other hi', bot)).toBe('@other hi');
  });
});

describe('isAddressedToBot — commands and casing', () => {
  const BOT = { id: 1, username: 'MyBot' };

  const cmd = (text: string): Message =>
    ({
      text,
      entities: [{ type: 'bot_command', offset: 0, length: text.split(' ')[0].length }],
    }) as Message;

  it('treats a command qualified with our username as addressed', () => {
    // Telegram emits this as ONE bot_command entity — there is no separate
    // mention entity — so a mention-only check silently dropped it.
    expect(isAddressedToBot(cmd('/settings@MyBot'), BOT)).toBe(true);
  });

  it('is case-insensitive about the command suffix', () => {
    expect(isAddressedToBot(cmd('/settings@mybot'), BOT)).toBe(true);
  });

  it('treats an unqualified command as addressed', () => {
    expect(isAddressedToBot(cmd('/settings'), BOT)).toBe(true);
  });

  it('ignores a command aimed at a different bot', () => {
    expect(isAddressedToBot(cmd('/settings@OtherBot'), BOT)).toBe(false);
  });

  it('keeps command arguments working', () => {
    expect(isAddressedToBot(cmd('/clean@MyBot all'), BOT)).toBe(true);
  });

  it('ignores a bot_command that is not at the start', () => {
    const text = 'look at /settings';
    const message = {
      text,
      entities: [{ type: 'bot_command', offset: 10, length: 9 }],
    } as Message;
    expect(isAddressedToBot(message, BOT)).toBe(false);
  });

  it('is case-insensitive about a plain @mention', () => {
    // stripBotMention already used the `i` flag; the detector did not, so a
    // lowercase mention was ignored while the stripper would have removed it.
    const text = '@mybot how is the weather?';
    const message = {
      text,
      entities: [{ type: 'mention', offset: 0, length: 6 }],
    } as Message;
    expect(isAddressedToBot(message, BOT)).toBe(true);
  });

  it('still ignores a mention of someone else', () => {
    const text = '@someoneelse hi';
    const message = {
      text,
      entities: [{ type: 'mention', offset: 0, length: 12 }],
    } as Message;
    expect(isAddressedToBot(message, BOT)).toBe(false);
  });
});
