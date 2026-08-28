/**
 * WHITELIST_PHONES parsing and matching.
 *
 * Keep byte-identical with the copies in `whatsapp-cloud` and `telegram-client`,
 * and semantically in sync with `_parse_whitelist` / `_is_whitelisted` in
 * `packages/ai-api/src/ai_api/routes/chat.py`.
 *
 * An entry is matched as EITHER a phone number OR a verbatim chat id. Under
 * Baileys v7 a chat is usually LID-addressed, so `remoteJid` is an anonymized
 * `@lid` whose digits are NOT a phone — a bare-phone entry can only match such a
 * chat via the resolved E.164 passed as `phone`, which is the whole point of
 * this module.
 *
 * Deliberately pure: no config, no logger, no jid.ts import (telegram-client has
 * no equivalent), so the three copies can stay identical and trivially testable.
 */

export interface Whitelist {
  /** Every entry verbatim (trimmed): `@lid`, `@g.us`, `tg:…`, full phone JIDs. */
  ids: Set<string>;
  /** Digit-normalized phone entries (no '+', no separators). */
  phones: Set<string>;
  /** Total non-empty entries; 0 means the whitelist is disabled. */
  size: number;
}

const PHONE_JID_SUFFIX = '@s.whatsapp.net';
const GROUP_JID_SUFFIX = '@g.us';
/** Only cosmetic separators — never "all non-digits", which would strip the
 *  sign off `tg:-100…` and leak ids into the phone set. */
const PHONE_PUNCTUATION = /[\s\-().]/g;
const DEVICE_SUFFIX = /:\d+@/;

/** Digits of a phone-shaped value, or null if it isn't one. */
function phoneDigits(value: string): string | null {
  const digits = value.replace(PHONE_PUNCTUATION, '').replace(/^\+/, '');
  return /^\d{5,}$/.test(digits) ? digits : null;
}

export function parseWhitelist(raw: string): Whitelist {
  const ids = new Set<string>();
  const phones = new Set<string>();
  let size = 0;

  for (const entry of raw.split(',')) {
    const trimmed = entry.trim();
    if (!trimmed) continue;
    size += 1;
    // Every entry is kept verbatim, so nothing an operator already relies on
    // can stop matching.
    ids.add(trimmed);

    if (trimmed.startsWith('tg:')) continue;
    const local = trimmed.endsWith(PHONE_JID_SUFFIX)
      ? trimmed.slice(0, -PHONE_JID_SUFFIX.length)
      : trimmed;
    // Any residual '@' means a non-phone scheme (@lid, @g.us, future ones).
    if (local.includes('@')) continue;

    const digits = phoneDigits(local);
    if (digits) phones.add(digits);
  }

  return { ids, phones, size };
}

/**
 * @param wl      parsed whitelist; `size === 0` disables it (allow everything)
 * @param chatJid the CONVERSATION's jid — never a group participant's
 * @param phone   the CONVERSATION's E.164 phone if known ('+49…'), else undefined
 *
 * Groups have no phone of their own, so callers pass `undefined` for them and a
 * group is matchable only by its `…@g.us` id. A participant's whitelisted phone
 * must never admit the whole group.
 */
export function isWhitelisted(wl: Whitelist, chatJid: string, phone?: string | null): boolean {
  if (wl.size === 0) return true;

  const jid = chatJid.replace(DEVICE_SUFFIX, '@');
  if (wl.ids.has(chatJid) || wl.ids.has(jid)) return true;

  // Legacy local-part match: what makes a bare `120363…` still admit a group and
  // a bare LID still admit its chat on existing installs.
  const at = jid.indexOf('@');
  if (at > 0) {
    const local = jid.slice(0, at);
    if (wl.phones.has(local) || wl.ids.has(local)) return true;
  }

  // A group is never admitted by a phone. Callers pass `undefined` for groups
  // anyway, but enforcing it here makes the rule structural rather than a
  // convention every call site has to remember.
  if (phone && !jid.endsWith(GROUP_JID_SUFFIX)) {
    const digits = phoneDigits(phone);
    if (digits && wl.phones.has(digits)) return true;
  }

  return false;
}
