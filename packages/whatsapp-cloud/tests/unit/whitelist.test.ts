/**
 * Unit tests for the Cloud API whitelist (webhook.ts:passesWhitelist).
 *
 * The Meta webhook payload carries only the sender's bare phone and no group
 * context, so the conversation is always private here. Group JID entries are
 * therefore inert on Cloud — a platform limitation, documented in CLAUDE.md.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const PHONE = '4915755945319';

describe('passesWhitelist (Cloud API)', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function loadWithWhitelist(value: string) {
    vi.stubEnv('WHITELIST_PHONES', value);
    const mod = await import('../../src/routes/webhook.js');
    return mod._internals.passesWhitelist;
  }

  it('allows everything when the whitelist is empty', async () => {
    const passesWhitelist = await loadWithWhitelist('');
    expect(passesWhitelist(PHONE)).toBe(true);
    expect(passesWhitelist('123')).toBe(true);
  });

  it('allows a whitelisted bare phone', async () => {
    const passesWhitelist = await loadWithWhitelist(PHONE);
    expect(passesWhitelist(PHONE)).toBe(true);
  });

  it('accepts cosmetic entry formats for the same number', async () => {
    for (const entry of [
      `+${PHONE}`,
      '+49 157 5594 5319',
      '49-157-5594-5319',
      `${PHONE}@s.whatsapp.net`,
    ]) {
      const passesWhitelist = await loadWithWhitelist(entry);
      expect(passesWhitelist(PHONE), entry).toBe(true);
      vi.resetModules();
    }
  });

  it('blocks a phone that is not listed', async () => {
    const passesWhitelist = await loadWithWhitelist(PHONE);
    expect(passesWhitelist('4915700000000')).toBe(false);
  });

  it('group JID entries have no effect on Cloud (no group context in the payload)', async () => {
    const passesWhitelist = await loadWithWhitelist('120363000000000000@g.us');
    expect(passesWhitelist(PHONE)).toBe(false);
    expect(passesWhitelist('120363000000000000')).toBe(false);
  });
});
