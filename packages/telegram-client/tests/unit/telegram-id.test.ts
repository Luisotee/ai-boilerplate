import { describe, it, expect } from 'vitest';
import {
  chatIdToJid,
  jidToChatId,
  isTelegramJid,
  chatTypeToConversationType,
} from '../../src/utils/telegram-id.js';

describe('chatIdToJid', () => {
  it('formats a positive chat id', () => {
    expect(chatIdToJid(123456789)).toBe('tg:123456789');
  });

  it('preserves negative supergroup ids', () => {
    expect(chatIdToJid(-1001234567890)).toBe('tg:-1001234567890');
  });
});

describe('jidToChatId', () => {
  it('parses a positive chat id', () => {
    expect(jidToChatId('tg:123456789')).toBe(123456789);
  });

  it('parses a negative supergroup id', () => {
    expect(jidToChatId('tg:-1001234567890')).toBe(-1001234567890);
  });

  it('throws on a non-telegram jid', () => {
    expect(() => jidToChatId('5511999999999@s.whatsapp.net')).toThrow(/not a telegram jid/i);
  });

  it('throws on a non-numeric suffix', () => {
    expect(() => jidToChatId('tg:abc')).toThrow(/invalid telegram chat id/i);
  });
});

describe('isTelegramJid', () => {
  it('recognizes a tg: prefix', () => {
    expect(isTelegramJid('tg:123')).toBe(true);
  });

  it('rejects whatsapp jids', () => {
    expect(isTelegramJid('5511999999999@s.whatsapp.net')).toBe(false);
    expect(isTelegramJid('120363000000000000@g.us')).toBe(false);
  });
});

describe('chatTypeToConversationType', () => {
  it('maps private to private', () => {
    expect(chatTypeToConversationType('private')).toBe('private');
  });

  it.each(['group', 'supergroup', 'channel'] as const)('maps %s to group', (t) => {
    expect(chatTypeToConversationType(t)).toBe('group');
  });
});
