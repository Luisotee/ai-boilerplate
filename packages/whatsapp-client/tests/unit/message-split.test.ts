import { describe, it, expect } from 'vitest';
import {
  splitResponseIntoBursts,
  stripSplitDelimiters,
  sleep,
} from '../../src/utils/message-split.js';

describe('splitResponseIntoBursts', () => {
  it('returns a single-element array when no delimiter is present', () => {
    const text = 'Hey, how are you? Just one thought.';
    expect(splitResponseIntoBursts(text)).toEqual([text]);
  });

  it('splits on a single `---` line into two trimmed chunks', () => {
    const text = 'Hey, how are you?\n---\nWanna go out tonight?';
    expect(splitResponseIntoBursts(text)).toEqual(['Hey, how are you?', 'Wanna go out tonight?']);
  });

  it('splits on multiple `---` lines preserving order', () => {
    const text = 'Hey, how are you?\n---\nWanna go out tonight?\n---\nGet some pizza or something.';
    expect(splitResponseIntoBursts(text)).toEqual([
      'Hey, how are you?',
      'Wanna go out tonight?',
      'Get some pizza or something.',
    ]);
  });

  it('does NOT split when `---` appears inside a fenced code block', () => {
    const text = 'Here is a config:\n```\nkey=value\n---\nother=1\n```\nall in one message';
    const result = splitResponseIntoBursts(text);
    expect(result).toHaveLength(1);
    expect(result[0]).toBe(text);
  });

  it('splits outside the fence but preserves the fence intact', () => {
    const text = 'First thought.\n---\nHere is code:\n```\na\n---\nb\n```\nDone.';
    const result = splitResponseIntoBursts(text);
    expect(result).toHaveLength(2);
    expect(result[0]).toBe('First thought.');
    expect(result[1]).toBe('Here is code:\n```\na\n---\nb\n```\nDone.');
  });

  it('returns single chunk when disabled is true even with delimiters', () => {
    const text = 'a\n---\nb\n---\nc';
    expect(splitResponseIntoBursts(text, { disabled: true })).toEqual([text]);
  });

  it('caps chunks at maxChunks by joining the tail with blank lines', () => {
    const text = 'one\n---\ntwo\n---\nthree\n---\nfour\n---\nfive\n---\nsix';
    const result = splitResponseIntoBursts(text, { maxChunks: 3 });
    expect(result).toHaveLength(3);
    expect(result[0]).toBe('one');
    expect(result[1]).toBe('two');
    expect(result[2]).toBe('three\n\nfour\n\nfive\n\nsix');
  });

  it('drops empty chunks from leading, trailing, and consecutive delimiters', () => {
    const text = '---\nfirst\n---\n---\nsecond\n---';
    expect(splitResponseIntoBursts(text)).toEqual(['first', 'second']);
  });

  it('returns original text for empty or whitespace-only input', () => {
    expect(splitResponseIntoBursts('')).toEqual(['']);
    expect(splitResponseIntoBursts('   \n\t\n')).toEqual(['   \n\t\n']);
  });

  it('returns original text when delimiter produces only one non-empty chunk', () => {
    // e.g. trailing `---` with no content after — should NOT force a split
    const text = 'just one thing\n---\n';
    expect(splitResponseIntoBursts(text)).toEqual([text]);
  });

  it('ignores `---` that is not on its own line', () => {
    const text = 'A line with --- inline text should not split.';
    expect(splitResponseIntoBursts(text)).toEqual([text]);
  });

  it('trims surrounding whitespace and blank lines within chunks', () => {
    const text = '\n\nfirst\n\n---\n\nsecond\n\n';
    expect(splitResponseIntoBursts(text)).toEqual(['first', 'second']);
  });

  it('clamps maxChunks to the hard cap when asked for something absurd', () => {
    const parts = Array.from({ length: 15 }, (_, i) => `msg${i + 1}`);
    const text = parts.join('\n---\n');
    const result = splitResponseIntoBursts(text, { maxChunks: 1000 });
    expect(result).toHaveLength(10); // HARD_CAP
  });
});

describe('stripSplitDelimiters', () => {
  it('removes `---` lines and collapses resulting blank-line runs', () => {
    const text = 'a\n---\nb\n---\nc';
    expect(stripSplitDelimiters(text)).toBe('a\n\nb\n\nc');
  });

  it('leaves text without delimiters alone', () => {
    expect(stripSplitDelimiters('hello world')).toBe('hello world');
  });

  it('collapses triple-or-more newlines to two', () => {
    expect(stripSplitDelimiters('a\n\n\n\nb')).toBe('a\n\nb');
  });

  it('trims leading and trailing whitespace', () => {
    expect(stripSplitDelimiters('\n\n---\nhello\n---\n\n')).toBe('hello');
  });
});

describe('sleep', () => {
  it('resolves after the given delay', async () => {
    const start = Date.now();
    await sleep(20);
    const elapsed = Date.now() - start;
    expect(elapsed).toBeGreaterThanOrEqual(15);
  });
});
