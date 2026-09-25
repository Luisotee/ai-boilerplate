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
/**
 * Only cosmetic separators — never "all non-digits", which would strip the sign
 * off `tg:-100…` and leak ids into the phone set.
 *
 * The whitespace half is the ECMAScript `\s` set written out literally rather
 * than as `\s`, so the Python mirror can reproduce it exactly: the two engines
 * disagree (Python's `\s` omits U+FEFF and adds U+0085 / U+001C–001F, and
 * `re.ASCII` drops NBSP and U+2000–200A). A backstop that accepts an entry the
 * gate rejects is the wrong direction, so both sides pin the same 25 codepoints.
 */
const PHONE_PUNCTUATION =
  /[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff\-().]/g;
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

    // A device-suffixed entry ("…:50@s.whatsapp.net") would otherwise be dead:
    // matching strips the suffix off the incoming jid, so the entry could never
    // equal it. Normalize the entry the same way — BEFORE the @s.whatsapp.net
    // strip, since the suffix sits in front of the '@'. Additive: the verbatim
    // form stays in `ids`. Safe by construction — DEVICE_SUFFIX requires an '@',
    // so `normalized` always contains one and is reachable only by the full-jid
    // clauses, never by the local-part ones. (Checked after the `tg:` continue,
    // so a Telegram id — always a possibly-negative integer, never an '@' — is
    // provably untouched rather than merely argued to be.)
    const normalized = trimmed.replace(DEVICE_SUFFIX, '@');
    if (normalized !== trimmed) ids.add(normalized);

    const local = normalized.endsWith(PHONE_JID_SUFFIX)
      ? normalized.slice(0, -PHONE_JID_SUFFIX.length)
      : normalized;
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
 * Groups have no phone of their own, so callers pass `undefined` for them, and
 * the `phone` ARGUMENT is never consulted for a group jid: a participant's
 * whitelisted phone must never admit the whole group. That is narrower than
 * "a group can only be matched by its `@g.us` id" — a legacy bare entry equal
 * to the group's id still admits it, via the id clause below.
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
    // Deliberately namespace-blind, because this IS the pre-split matcher:
    // the whole check used to be `jid.split('@')[0] in whitelist`. It is what
    // keeps a bare `120363…` admitting its group and a bare LID admitting its
    // chat on existing installs — the same code path for both, since nothing
    // distinguishes a bare group id from a bare phone (`\d{5,}` matches both).
    // Cannot be narrowed without breaking documented behaviour; pinned by test.
    if (wl.ids.has(local)) return true;
    // Normalized digits, though, are only ever a phone. Consulting them for an
    // @lid / @g.us / @broadcast local part would let a phone entry admit an
    // unrelated namespace that merely shares digits — so, phone JIDs only.
    if (jid.endsWith(PHONE_JID_SUFFIX) && wl.phones.has(local)) return true;
  }

  // The `phone` argument is never consulted for a group. Callers pass
  // `undefined` for groups anyway, but enforcing it here makes THAT rule
  // structural rather than a convention every call site has to remember.
  // Independent of the guard above: this clause reads the caller-supplied
  // E.164, that one reads the jid's own local part.
  if (phone && !jid.endsWith(GROUP_JID_SUFFIX)) {
    const digits = phoneDigits(phone);
    if (digits && wl.phones.has(digits)) return true;
  }

  return false;
}
