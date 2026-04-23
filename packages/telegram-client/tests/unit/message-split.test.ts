import { describe, it, expect } from 'vitest';
import {
  splitResponseIntoBursts,
  stripSplitDelimiters,
  sleep,
} from '../../src/utils/message-split.js';

describe('splitResponseIntoBursts', () => {
  it('returns the original text when there are no delimiters', () => {
    expect(splitResponseIntoBursts('hello world')).toEqual(['hello world']);
  });

  it('splits on "---" delimiter lines', () => {
    const input = 'one\n---\ntwo\n---\nthree';
    expect(splitResponseIntoBursts(input)).toEqual(['one', 'two', 'three']);
  });

  it('does not split inside fenced code blocks', () => {
    const input = '```\n---\n```\noutside';
    expect(splitResponseIntoBursts(input)).toEqual([input]);
  });

  it('caps at maxChunks by concatenating the tail', () => {
    const input = 'a\n---\nb\n---\nc\n---\nd\n---\ne';
    expect(splitResponseIntoBursts(input, { maxChunks: 3 })).toEqual(['a', 'b', 'c\n\nd\n\ne']);
  });

  it('returns a single-element array when disabled, stripping delimiters', () => {
    const input = 'one\n---\ntwo';
    expect(splitResponseIntoBursts(input, { disabled: true })).toEqual(['one\n\ntwo']);
  });
});

describe('stripSplitDelimiters', () => {
  it('is a no-op when there are no delimiters', () => {
    expect(stripSplitDelimiters('hi')).toBe('hi');
  });

  it('removes --- lines and collapses blank runs', () => {
    expect(stripSplitDelimiters('a\n---\nb')).toBe('a\n\nb');
  });
});

describe('sleep', () => {
  it('resolves after roughly the requested delay', async () => {
    const start = Date.now();
    await sleep(10);
    expect(Date.now() - start).toBeGreaterThanOrEqual(5);
  });
});
