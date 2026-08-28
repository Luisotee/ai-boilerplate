/**
 * Unit tests for the Telegram client whitelist (updates.ts:passesWhitelist).
 *
 * The function keys on the chat ID — meaning:
 *   - Private chats: chat_id == user_id, so whitelisting "tg:<user_id>" lets
 *     the user message the bot privately.
 *   - Groups/supergroups: chat_id is the GROUP id (negative for supergroups),
 *     not the sender's user id. Whitelisting an individual user has NO effect
 *     for messages they send in a group — the GROUP must be whitelisted.
 *
 * This mirrors the documented Cloud API limitation in CLAUDE.md.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

describe('passesWhitelist (Telegram)', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function loadInternalsWithWhitelist(value: string) {
    vi.stubEnv('WHITELIST_PHONES', value);
    const mod = await import('../../src/updates.js');
    return mod._internals.passesWhitelist;
  }

  it('allows everything when the whitelist is empty', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('');
    expect(passesWhitelist(123)).toBe(true);
    expect(passesWhitelist(-1001234567890)).toBe(true);
    expect(passesWhitelist(undefined)).toBe(true);
  });

  it('allows a whitelisted private user (tg:<user_id>)', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('tg:42');
    expect(passesWhitelist(42)).toBe(true);
    expect(passesWhitelist(43)).toBe(false);
  });

  it('allows a whitelisted supergroup (tg:-100...)', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('tg:-1001234567890');
    expect(passesWhitelist(-1001234567890)).toBe(true);
    expect(passesWhitelist(-1009999999999)).toBe(false);
  });

  it('does NOT allow group messages just because the SENDER is whitelisted', async () => {
    // The whitelist contains the user's tg: JID, but the chat_id passed to
    // passesWhitelist is the GROUP's id — so the message is filtered.
    const passesWhitelist = await loadInternalsWithWhitelist('tg:42');
    expect(passesWhitelist(-1001234567890)).toBe(false);
  });

  it('rejects when chatId is undefined and the whitelist is non-empty', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('tg:42');
    expect(passesWhitelist(undefined)).toBe(false);
  });

  it('handles multiple whitelist entries (user + group)', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('tg:42, tg:-1001234567890');
    expect(passesWhitelist(42)).toBe(true);
    expect(passesWhitelist(-1001234567890)).toBe(true);
    expect(passesWhitelist(-1009999999999)).toBe(false);
    expect(passesWhitelist(99)).toBe(false);
  });

  // A phone-shaped entry lives in a separate set that the Telegram path never
  // consults, so it can never admit a chat whose id happens to have the same
  // digits. Mirrors test_bare_chat_id_does_not_match_tg_jid in the AI API.
  it('does NOT allow a chat from a bare number with the same digits', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('123456789');
    expect(passesWhitelist(123456789)).toBe(false);
  });

  it('keeps phone and tg: entries separate in a mixed whitelist', async () => {
    const passesWhitelist = await loadInternalsWithWhitelist('4915755945319, tg:42');
    expect(passesWhitelist(42)).toBe(true);
    expect(passesWhitelist(4915755945319)).toBe(false);
  });
});
