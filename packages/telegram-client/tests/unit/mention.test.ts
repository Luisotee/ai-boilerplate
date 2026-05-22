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
        { type: 'text_mention', offset: 4, length: 5, user: { id: 42, is_bot: true, first_name: 'My Bot' } },
      ],
    });
    expect(isAddressedToBot(msg, bot)).toBe(true);
  });

  it('ignores text_mention for other users', () => {
    const msg = makeTextMessage('hey someone', {
      entities: [
        { type: 'text_mention', offset: 4, length: 7, user: { id: 99, is_bot: false, first_name: 'Other' } },
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
