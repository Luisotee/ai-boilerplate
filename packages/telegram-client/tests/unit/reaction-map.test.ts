import { describe, it, expect } from 'vitest';
import { _internals } from '../../src/services/telegram-api.js';

const { substituteReaction, REACTION_MAP } = _internals;

describe('reaction emoji substitution', () => {
  it('maps the three WhatsApp status emojis to allowed Telegram reactions', () => {
    expect(substituteReaction('⏳')).toBe('🤔');
    expect(substituteReaction('✅')).toBe('👍');
    expect(substituteReaction('❌')).toBe('👎');
  });

  it('passes already-allowed emojis through unchanged', () => {
    expect(substituteReaction('👍')).toBe('👍');
    expect(substituteReaction('❤')).toBe('❤');
    expect(substituteReaction('🎉')).toBe('🎉');
  });

  it('exposes the substitution map for reference', () => {
    expect(REACTION_MAP).toEqual({ '⏳': '🤔', '✅': '👍', '❌': '👎' });
  });
});
