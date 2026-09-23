/**
 * Pure whitelist-gating decision for an incoming message.
 *
 * Keep byte-identical with the copy in `telegram-client` (enforced by
 * `whatsapp-client/tests/unit/whitelist-copies.test.ts`). Deliberately pure — no
 * config, no logger, no I/O — so both clients share one truth table and it can
 * be unit-tested directly. The async inputs (group membership, @mention
 * detection) are resolved by the caller.
 *
 * GROUP_GATING selects how a GROUP gets in scope when WHITELIST_PHONES is set:
 *
 *   - `jid` (default): a group is in scope only when its own chat id is listed.
 *     Every member of a listed group can address the bot. This is the
 *     historical behaviour.
 *   - `membership`: a group is in scope when it has a whitelisted member (Baileys
 *     checks the participant list; the Telegram Bot API cannot enumerate
 *     members, so there every group the bot is in is in scope). The bot SAVES
 *     every message of an in-scope group but only REPLIES to a whitelisted
 *     sender. An explicitly listed group still answers everyone.
 *
 * Both modes feed the same table: in `jid` mode the caller simply passes
 * `groupAllowed = groupExplicit`, which makes `suppressResponse` unreachable.
 */

export type GroupGatingMode = 'jid' | 'membership';

/** Parse GROUP_GATING. Anything unrecognised falls back to the stricter `jid`. */
export function parseGroupGating(raw: string | undefined): {
  mode: GroupGatingMode;
  invalid: boolean;
} {
  const value = (raw ?? '').trim().toLowerCase();
  if (value === '' || value === 'jid') return { mode: 'jid', invalid: false };
  if (value === 'membership') return { mode: 'membership', invalid: false };
  return { mode: 'jid', invalid: true };
}

export interface GateInputs {
  /** Is this a group chat (vs a 1:1)? */
  isGroup: boolean;
  /** Is the whitelist active (WHITELIST_PHONES non-empty)? */
  whitelistEnabled: boolean;
  /** Is the message's sender a whitelisted identity? (For a 1:1, the chat itself.) */
  senderWhitelisted: boolean;
  /** Is the group itself explicitly whitelisted (its chat id in WHITELIST_PHONES)? */
  groupExplicit: boolean;
  /**
   * Is the group in scope? `jid` mode: same as `groupExplicit`. `membership`
   * mode: explicit, or the sender is whitelisted, or the group has a
   * whitelisted member — resolved by the caller, which owns the short-circuit
   * that avoids a metadata fetch.
   */
  groupAllowed: boolean;
}

export interface Gate {
  /** Drop the message entirely (not saved, not answered). */
  skip: boolean;
  /** Group kept for context, but this sender may not trigger a reply. */
  suppressResponse: boolean;
}

/**
 * The part of the decision that does NOT depend on @mention detection, so a
 * caller can drop a message before touching anything else (Baileys must gate
 * before it dereferences `sock.user`).
 */
export function gateMessage(i: GateInputs): Gate {
  if (i.whitelistEnabled) {
    if (i.isGroup) {
      if (!i.groupAllowed) return { skip: true, suppressResponse: false };
    } else if (!i.senderWhitelisted) {
      return { skip: true, suppressResponse: false };
    }
  }
  const suppressResponse =
    i.isGroup && i.whitelistEnabled && !i.senderWhitelisted && !i.groupExplicit;
  return { skip: false, suppressResponse };
}

export interface GroupGatingInputs extends GateInputs {
  /** Is the bot addressed in this group (@mention / reply-to-bot / command)? */
  respondInGroup: boolean;
}

export interface GroupGatingDecision {
  /** Drop the message entirely (not saved, not answered). */
  skip: boolean;
  /** Save to history without generating a response. */
  saveOnly: boolean;
}

/**
 * Full decision: skip, or keep and whether it is save-only. A kept group
 * message is save-only when it is not addressed to the bot, or when its sender
 * is not entitled to a reply. A 1:1 is never save-only.
 */
export function decideGroupGating(i: GroupGatingInputs): GroupGatingDecision {
  const gate = gateMessage(i);
  if (gate.skip) return { skip: true, saveOnly: false };
  return { skip: false, saveOnly: i.isGroup && (gate.suppressResponse || !i.respondInGroup) };
}
