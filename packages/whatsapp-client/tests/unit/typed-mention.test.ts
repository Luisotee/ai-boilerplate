import { describe, it, expect } from 'vitest';
import { isTypedMention, normalizeMentionNames } from '../../src/utils/typed-mention.js';

describe('isTypedMention', () => {
  const names = ['Curupira', '5511000000000'];

  it.each([
    ['@Curupira tudo bem?', true],
    ['oi @curupira', true],
    ['oi @CURUPIRA!', true],
    ['(@Curupira) vê isso', true],
    ['@5511000000000 pode ver?', true],
    ['@Curupira, e aí?', true],
    ['@Curupira.', true],
  ])('matches %j', (text, expected) => {
    expect(isTypedMention(text, names)).toBe(expected);
  });

  it.each([
    ['curupira sem arroba', false],
    ['@Curupiras não é o bot', false],
    ['@Curupira_bot é outro', false],
    ['@Curupira2', false],
    ['mail me at x@Curupira.com', false],
    ['https://example.com/@Curupira', false],
    ['x.@Curupira', false],
    ['@@Curupira', false],
    ['@551100000000099', false],
    ['', false],
  ])('does not match %j', (text, expected) => {
    expect(isTypedMention(text, names)).toBe(expected);
  });

  it('respects Unicode word boundaries', () => {
    expect(isTypedMention('@José bom dia', ['José'])).toBe(true);
    expect(isTypedMention('@Josélia bom dia', ['José'])).toBe(false);
    expect(isTypedMention('é@José', ['José'])).toBe(false);
  });

  it('matches multi-word names and escapes regex characters', () => {
    expect(isTypedMention('@Assistente Virtual ajuda', ['Assistente Virtual'])).toBe(true);
    expect(isTypedMention('@a.b ok', ['a.b'])).toBe(true);
    expect(isTypedMention('@axb ok', ['a.b'])).toBe(false);
  });

  it('ignores names that are too short or empty', () => {
    expect(isTypedMention('@a hi', ['a', '', undefined, null])).toBe(false);
  });
});

describe('normalizeMentionNames', () => {
  it('trims, drops a leading @, dedupes case-insensitively and skips short names', () => {
    expect(normalizeMentionNames([' Curupira ', '@curupira', 'x', undefined, 'Bot'])).toEqual([
      'Curupira',
      'Bot',
    ]);
  });
});
