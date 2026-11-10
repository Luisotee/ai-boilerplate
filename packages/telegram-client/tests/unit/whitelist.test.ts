/**
 * Unit tests for the Telegram client whitelist, through the gate every handler
 * runs (updates.ts:resolveGate): `passes(chatId)` is `!skip`.
 *
 * The gate keys on the chat ID — meaning:
 *   - Private chats: chat_id == user_id, so whitelisting "tg:<user_id>" lets
 *     the user message the bot privately.
 *   - Groups/supergroups: chat_id is the GROUP id (negative for supergroups),
 *     not the sender's user id. Whitelisting an individual user has NO effect
 *     for messages they send in a group — the GROUP must be whitelisted.
 *
 * This mirrors the documented Cloud API limitation in CLAUDE.md.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// The whitelist comes from the AI API now; serve WHITELIST_PHONES from the env.
vi.mock('../../src/services/live-whitelist.js', async () =>
  (await import('../helpers/live-whitelist-env.js')).envWhitelistMock()
);

describe('resolveGate whitelist (Telegram)', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function loadInternalsWithWhitelist(value: string) {
    vi.stubEnv('WHITELIST_PHONES', value);
    const mod = await import('../../src/updates.js');
    // A negative id is a supergroup (jid gating, un-addressed), else private.
    return async (chatId: number | undefined): Promise<boolean> => {
      const chat =
        chatId === undefined
          ? undefined
          : { id: chatId, type: chatId < 0 ? 'supergroup' : 'private' };
      const ctx = { chat, update: { update_id: 1 } } as unknown as Parameters<
        typeof mod._internals.resolveGate
      >[0];
      return !(await mod._internals.resolveGate(ctx)).skip;
    };
  }

  it('FAILS CLOSED when the whitelist is unavailable from the AI API', async () => {
    vi.stubEnv('WHITELIST_UNAVAILABLE', '1');
    const passes = await loadInternalsWithWhitelist('');
    expect(await passes(42)).toBe(false);
  });

  it('allows everything when the whitelist is empty', async () => {
    const passes = await loadInternalsWithWhitelist('');
    expect(await passes(123)).toBe(true);
    expect(await passes(-1001234567890)).toBe(true);
    expect(await passes(undefined)).toBe(true);
  });

  it('allows a whitelisted private user (tg:<user_id>)', async () => {
    const passes = await loadInternalsWithWhitelist('tg:42');
    expect(await passes(42)).toBe(true);
    expect(await passes(43)).toBe(false);
  });

  it('allows a whitelisted supergroup (tg:-100...)', async () => {
    const passes = await loadInternalsWithWhitelist('tg:-1001234567890');
    expect(await passes(-1001234567890)).toBe(true);
    expect(await passes(-1009999999999)).toBe(false);
  });

  it('does NOT allow group messages just because the SENDER is whitelisted', async () => {
    // The whitelist contains the user's tg: JID, but the chat_id the gate
    // checks is the GROUP's id — so the message is filtered.
    const passes = await loadInternalsWithWhitelist('tg:42');
    expect(await passes(-1001234567890)).toBe(false);
  });

  it('rejects when chatId is undefined and the whitelist is non-empty', async () => {
    const passes = await loadInternalsWithWhitelist('tg:42');
    expect(await passes(undefined)).toBe(false);
  });

  it('handles multiple whitelist entries (user + group)', async () => {
    const passes = await loadInternalsWithWhitelist('tg:42, tg:-1001234567890');
    expect(await passes(42)).toBe(true);
    expect(await passes(-1001234567890)).toBe(true);
    expect(await passes(-1009999999999)).toBe(false);
    expect(await passes(99)).toBe(false);
  });

  // A phone-shaped entry lives in a separate set that the Telegram path never
  // consults, so it can never admit a chat whose id happens to have the same
  // digits. Mirrors test_bare_chat_id_does_not_match_tg_jid in the AI API.
  it('does NOT allow a chat from a bare number with the same digits', async () => {
    const passes = await loadInternalsWithWhitelist('123456789');
    expect(await passes(123456789)).toBe(false);
  });

  it('keeps phone and tg: entries separate in a mixed whitelist', async () => {
    const passes = await loadInternalsWithWhitelist('4915755945319, tg:42');
    expect(await passes(42)).toBe(true);
    expect(await passes(4915755945319)).toBe(false);
  });
});
