/**
 * Unit tests for utils/gating.ts — the pure whitelist-gating truth table shared
 * by the Baileys and Telegram clients (byte-identical copies).
 *
 * Locks in the privacy contract: who is skipped, and which allowed messages are
 * saved-only vs answered. Especially: under GROUP_GATING=membership an @mention
 * from a non-whitelisted sender in an in-scope group must be SAVED but NOT
 * answered.
 */

import { describe, it, expect } from 'vitest';
import {
  decideGroupGating,
  gateMessage,
  parseGroupGating,
  type GroupGatingInputs,
} from '../../src/utils/gating.js';

function inputs(overrides: Partial<GroupGatingInputs> = {}): GroupGatingInputs {
  return {
    isGroup: false,
    whitelistEnabled: false,
    senderWhitelisted: false,
    groupExplicit: false,
    groupAllowed: false,
    respondInGroup: false,
    ...overrides,
  };
}

describe('parseGroupGating', () => {
  it.each([
    [undefined, 'jid', false],
    ['', 'jid', false],
    ['jid', 'jid', false],
    [' Membership ', 'membership', false],
    ['members', 'jid', true],
  ])('%j -> %s (invalid=%s)', (raw, mode, invalid) => {
    expect(parseGroupGating(raw as string | undefined)).toEqual({ mode, invalid });
  });
});

describe('decideGroupGating — whitelist disabled', () => {
  it('keeps a private message (not save-only)', () => {
    expect(decideGroupGating(inputs({ isGroup: false }))).toEqual({ skip: false, saveOnly: false });
  });

  it('saves a group message with no @mention', () => {
    expect(decideGroupGating(inputs({ isGroup: true, respondInGroup: false }))).toEqual({
      skip: false,
      saveOnly: true,
    });
  });

  it('answers a group @mention', () => {
    expect(decideGroupGating(inputs({ isGroup: true, respondInGroup: true }))).toEqual({
      skip: false,
      saveOnly: false,
    });
  });
});

describe('decideGroupGating — whitelist enabled, private', () => {
  it('skips a non-whitelisted sender', () => {
    expect(decideGroupGating(inputs({ whitelistEnabled: true, senderWhitelisted: false }))).toEqual(
      { skip: true, saveOnly: false }
    );
  });

  it('keeps a whitelisted sender', () => {
    expect(decideGroupGating(inputs({ whitelistEnabled: true, senderWhitelisted: true }))).toEqual({
      skip: false,
      saveOnly: false,
    });
  });
});

describe('decideGroupGating — whitelist enabled, group', () => {
  it('skips a group that is not in scope', () => {
    expect(
      decideGroupGating(inputs({ isGroup: true, whitelistEnabled: true, groupAllowed: false }))
    ).toEqual({ skip: true, saveOnly: false });
  });

  it('answers an @mention from a whitelisted sender', () => {
    expect(
      decideGroupGating(
        inputs({
          isGroup: true,
          whitelistEnabled: true,
          senderWhitelisted: true,
          groupAllowed: true,
          respondInGroup: true,
        })
      )
    ).toEqual({ skip: false, saveOnly: false });
  });

  it('saves (but does not answer) a whitelisted sender with no @mention', () => {
    expect(
      decideGroupGating(
        inputs({
          isGroup: true,
          whitelistEnabled: true,
          senderWhitelisted: true,
          groupAllowed: true,
          respondInGroup: false,
        })
      )
    ).toEqual({ skip: false, saveOnly: true });
  });

  it('PRIVACY: @mention from a NON-whitelisted sender in an in-scope group is saved, NOT answered', () => {
    expect(
      decideGroupGating(
        inputs({
          isGroup: true,
          whitelistEnabled: true,
          senderWhitelisted: false,
          groupExplicit: false,
          groupAllowed: true,
          respondInGroup: true,
        })
      )
    ).toEqual({ skip: false, saveOnly: true });
  });

  it('answers an @mention in an explicitly-whitelisted group regardless of sender', () => {
    expect(
      decideGroupGating(
        inputs({
          isGroup: true,
          whitelistEnabled: true,
          senderWhitelisted: false,
          groupExplicit: true,
          groupAllowed: true,
          respondInGroup: true,
        })
      )
    ).toEqual({ skip: false, saveOnly: false });
  });
});

describe('jid mode (groupAllowed = groupExplicit) reproduces the historical gate', () => {
  it.each([
    [true, { skip: false, suppressResponse: false }],
    [false, { skip: true, suppressResponse: false }],
  ])('group listed=%s', (listed, expected) => {
    expect(
      gateMessage({
        isGroup: true,
        whitelistEnabled: true,
        senderWhitelisted: listed,
        groupExplicit: listed,
        groupAllowed: listed,
      })
    ).toEqual(expected);
  });

  it('never suppresses a reply in a listed group', () => {
    expect(
      gateMessage({
        isGroup: true,
        whitelistEnabled: true,
        senderWhitelisted: true,
        groupExplicit: true,
        groupAllowed: true,
      }).suppressResponse
    ).toBe(false);
  });
});
