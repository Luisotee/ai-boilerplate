import { describe, it, expect } from 'vitest';
import { createMentionWindow } from '../../src/utils/mention-window.js';

describe('createMentionWindow', () => {
  it('admits the next message of the same key within the TTL, once', () => {
    const w = createMentionWindow(60_000);
    w.arm('g|a', 1_000);
    expect(w.consume('g|a', 30_000)).toBe(true);
    expect(w.consume('g|a', 31_000)).toBe(false);
  });

  it('does not admit another sender or another group', () => {
    const w = createMentionWindow(60_000);
    w.arm('g|a', 0);
    expect(w.consume('g|b', 1)).toBe(false);
    expect(w.consume('h|a', 1)).toBe(false);
    expect(w.consume('g|a', 1)).toBe(true);
  });

  it('expires after the TTL', () => {
    const w = createMentionWindow(60_000);
    w.arm('g|a', 0);
    expect(w.consume('g|a', 60_000)).toBe(false);
    expect(w.size()).toBe(0);
  });

  it('re-arming extends the window', () => {
    const w = createMentionWindow(60_000);
    w.arm('g|a', 0);
    w.arm('g|a', 50_000);
    expect(w.consume('g|a', 100_000)).toBe(true);
  });

  it('is disabled by a TTL of 0', () => {
    const w = createMentionWindow(0);
    w.arm('g|a', 0);
    expect(w.consume('g|a', 0)).toBe(false);
    expect(w.size()).toBe(0);
  });

  it('prunes expired entries and stays within maxEntries', () => {
    const w = createMentionWindow(10, 3);
    w.arm('a', 0);
    w.arm('b', 100); // 'a' expired by now
    expect(w.size()).toBe(1);
    w.arm('c', 100);
    w.arm('d', 100);
    w.arm('e', 100);
    expect(w.size()).toBe(3);
    expect(w.consume('b', 101)).toBe(false); // the oldest was dropped
    expect(w.consume('e', 101)).toBe(true);
  });
});
