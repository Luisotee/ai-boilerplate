import { describe, it, expect } from 'vitest';
import {
  MAX_MESSAGE_CHARS,
  splitByLength,
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

describe('length capping (Telegram 4096 limit)', () => {
  const long = (n: number) => 'a'.repeat(n);

  it('returns a short text unchanged', () => {
    expect(splitByLength('hello')).toEqual(['hello']);
  });

  it('splits text with no delimiters at all', () => {
    // The original bug: a long answer with no `---` was returned as ONE chunk
    // and rejected wholesale by Telegram with a 400.
    const chunks = splitResponseIntoBursts(long(10_000));
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(MAX_MESSAGE_CHARS);
  });

  it('caps even when bursting is disabled — the group path', () => {
    // text.ts disables bursting for groups, which is exactly where long
    // answers were guaranteed to hit the limit.
    const chunks = splitResponseIntoBursts(long(10_000), { disabled: true });
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(MAX_MESSAGE_CHARS);
  });

  it('caps the merged tail chunk', () => {
    // Tail-merging can produce an over-long chunk even when each part fit,
    // so the length pass has to run after the merge.
    const part = `${long(2000)}\n---\n`;
    const chunks = splitResponseIntoBursts(part.repeat(8), { maxChunks: 2 });
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(MAX_MESSAGE_CHARS);
  });

  it('prefers a paragraph boundary', () => {
    // Must exceed 4096 overall, or there is nothing to split.
    const text = `${long(4090)}\n\nsecond paragraph`;
    const chunks = splitByLength(text);
    expect(chunks[0]).toBe(long(4090));
    expect(chunks[1]).toBe('second paragraph');
  });

  it('falls back to a line boundary', () => {
    const text = `${long(4090)}\nnext line`;
    expect(splitByLength(text)[1]).toBe('next line');
  });

  it('falls back to a word boundary', () => {
    const text = `${long(4095)} tail`;
    const chunks = splitByLength(text);
    expect(chunks[0]).toBe(long(4095));
    expect(chunks[1]).toBe('tail');
  });

  it('hard-cuts input with no whitespace (long URL / base64 blob)', () => {
    const chunks = splitByLength(long(9000));
    expect(chunks.length).toBe(3);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(MAX_MESSAGE_CHARS);
    expect(chunks.join('')).toBe(long(9000));
  });

  it('preserves all non-whitespace content across the split', () => {
    const text = Array.from({ length: 900 }, (_, i) => `line ${i} with accents: ção`).join('\n');
    const chunks = splitByLength(text);
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.join('\n').replace(/\s+/g, '')).toBe(text.replace(/\s+/g, ''));
  });

  it('handles a realistic oversized /memories document', () => {
    // The sharpest trigger: /memories returns the whole unbounded core-memory
    // document, so past ~4096 chars the command broke permanently.
    const doc =
      'Your memories:\n\n' +
      Array.from({ length: 400 }, (_, i) => `- Fact ${i} about the user.`).join('\n');
    expect(doc.length).toBeGreaterThan(MAX_MESSAGE_CHARS);

    const chunks = splitResponseIntoBursts(doc, { disabled: true });
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(MAX_MESSAGE_CHARS);
    expect(chunks[0]).toContain('Your memories:');
  });

  it('never emits an empty chunk', () => {
    for (const input of [long(4096), long(4097), `${long(4096)}\n\n\n\n${long(10)}`]) {
      for (const c of splitByLength(input)) expect(c.length).toBeGreaterThan(0);
    }
  });

  it('terminates on pathological whitespace-only padding', () => {
    const chunks = splitByLength(`${long(4000)}${' '.repeat(5000)}end`);
    expect(chunks.length).toBeGreaterThan(0);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(MAX_MESSAGE_CHARS);
  });
});
