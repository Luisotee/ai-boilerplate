import { describe, it, expect } from 'vitest';
import { parseWhitelist, isWhitelisted } from '../../src/utils/whitelist.js';

const PHONE = '4915755945319';
const PHONE_JID = `${PHONE}@s.whatsapp.net`;
const LID = '109994229891095@lid';
const GROUP = '120363000000000000@g.us';

describe('parseWhitelist', () => {
  it('reports an empty whitelist as size 0', () => {
    for (const raw of ['', '   ', ',', ' , , ']) {
      expect(parseWhitelist(raw).size).toBe(0);
    }
  });

  it('classifies phone entries into phones and every entry into ids', () => {
    const wl = parseWhitelist(`${PHONE}, ${GROUP} , tg:42`);
    expect(wl.size).toBe(3);
    expect(wl.phones).toEqual(new Set([PHONE]));
    expect(wl.ids).toEqual(new Set([PHONE, GROUP, 'tg:42']));
  });

  it('never lets an id-shaped entry leak into the phone set', () => {
    const wl = parseWhitelist(`tg:-1001234567890, ${LID}, ${GROUP}`);
    expect(wl.phones.size).toBe(0);
  });
});

describe('isWhitelisted', () => {
  it('allows everything when the whitelist is empty', () => {
    const wl = parseWhitelist('');
    expect(isWhitelisted(wl, PHONE_JID)).toBe(true);
    expect(isWhitelisted(wl, LID)).toBe(true);
    expect(isWhitelisted(wl, GROUP, undefined)).toBe(true);
  });

  it('allows a phone-addressed chat from a bare phone entry', () => {
    expect(isWhitelisted(parseWhitelist(PHONE), PHONE_JID)).toBe(true);
  });

  // The headline bug: under Baileys v7 the chat is LID-addressed, so the JID
  // digits are an anonymized account id. Only the resolved phone can match.
  it('allows a LID-addressed chat from a bare phone entry when the phone resolved', () => {
    expect(isWhitelisted(parseWhitelist(PHONE), LID, `+${PHONE}`)).toBe(true);
  });

  it('fails closed on a LID whose phone could not be resolved', () => {
    expect(isWhitelisted(parseWhitelist(PHONE), LID, undefined)).toBe(false);
    expect(isWhitelisted(parseWhitelist(PHONE), LID, null)).toBe(false);
  });

  it('allows an unresolvable LID that is listed verbatim (the escape hatch)', () => {
    expect(isWhitelisted(parseWhitelist(LID), LID, undefined)).toBe(true);
  });

  it('strips the device suffix before matching', () => {
    expect(isWhitelisted(parseWhitelist(PHONE), `${PHONE}:50@s.whatsapp.net`)).toBe(true);
    expect(isWhitelisted(parseWhitelist(GROUP), '120363000000000000:12@g.us')).toBe(true);
  });

  it('tolerates cosmetic entry formats', () => {
    for (const entry of [
      `+${PHONE}`,
      '+49 157 5594 5319',
      '49-157-5594-5319',
      '(49) 157 5594 5319',
      PHONE_JID,
    ]) {
      expect(isWhitelisted(parseWhitelist(entry), PHONE_JID), entry).toBe(true);
      expect(isWhitelisted(parseWhitelist(entry), LID, `+${PHONE}`), entry).toBe(true);
    }
  });

  it('matches a group by its JID', () => {
    expect(isWhitelisted(parseWhitelist(GROUP), GROUP, undefined)).toBe(true);
  });

  // A group has no phone of its own; a participant's phone must never admit it.
  it('does NOT allow a group just because a participant phone is whitelisted', () => {
    expect(isWhitelisted(parseWhitelist(PHONE), GROUP, `+${PHONE}`)).toBe(false);
  });

  it('keeps legacy bare-local-part entries working', () => {
    expect(isWhitelisted(parseWhitelist('120363000000000000'), GROUP)).toBe(true);
    expect(isWhitelisted(parseWhitelist('109994229891095'), LID)).toBe(true);
  });

  it('rejects chats that are not listed', () => {
    const wl = parseWhitelist(`${PHONE}, ${GROUP}`);
    expect(isWhitelisted(wl, '4915700000000@s.whatsapp.net')).toBe(false);
    expect(isWhitelisted(wl, '999999999999999@lid', '+4915700000000')).toBe(false);
    expect(isWhitelisted(wl, '120363999999999999@g.us')).toBe(false);
  });

  it('does not let a bare number match a Telegram chat id', () => {
    expect(isWhitelisted(parseWhitelist('123456789'), 'tg:123456789')).toBe(false);
  });
});

describe('namespace scoping of the phone set', () => {
  // A phone entry's NORMALIZED digits must not admit a same-digit chat in
  // another namespace. Pre-split these entries matched nothing at all, so
  // narrowing them cannot regress anyone.
  it('a phone-JID entry does not admit the same digits as a LID', () => {
    expect(isWhitelisted(parseWhitelist(PHONE_JID), `${PHONE}@lid`)).toBe(false);
  });

  it('a cosmetic phone entry does not admit the same digits elsewhere', () => {
    for (const jid of [`${PHONE}@lid`, `${PHONE}@g.us`, `${PHONE}@broadcast`]) {
      expect(isWhitelisted(parseWhitelist('+49 157 5594 5319'), jid), jid).toBe(false);
    }
  });

  it('but still admits its intended phone chat', () => {
    expect(isWhitelisted(parseWhitelist('+49 157 5594 5319'), PHONE_JID)).toBe(true);
    expect(isWhitelisted(parseWhitelist('+49 157 5594 5319'), LID, `+${PHONE}`)).toBe(true);
  });

  // PINNED, NOT A BUG. A BARE entry is an untyped local part and stays
  // namespace-blind: that is exactly what `jid.split('@')[0] in whitelist` did
  // before the two-set split, and it is the same code path that keeps a bare
  // `120363…` admitting its group. Nothing distinguishes a bare group id from
  // a bare phone. Do NOT "fix" this without a migration note.
  it('pins the legacy namespace-blind bare-entry match', () => {
    for (const jid of [`${PHONE}@lid`, `${PHONE}@g.us`, `${PHONE}@broadcast`]) {
      expect(isWhitelisted(parseWhitelist(PHONE), jid), jid).toBe(true);
    }
  });
});

describe('device-suffixed entries', () => {
  const DEV_ENTRY = `${PHONE}:50@s.whatsapp.net`;

  it('parses to both the verbatim and the stripped id, plus the phone', () => {
    const wl = parseWhitelist(DEV_ENTRY);
    expect(wl.ids).toEqual(new Set([DEV_ENTRY, PHONE_JID]));
    expect(wl.phones).toEqual(new Set([PHONE]));
    expect(wl.size).toBe(1);
  });

  it('matches the plain phone JID and a resolved LID', () => {
    expect(isWhitelisted(parseWhitelist(DEV_ENTRY), PHONE_JID)).toBe(true);
    expect(isWhitelisted(parseWhitelist(DEV_ENTRY), LID, `+${PHONE}`)).toBe(true);
  });

  it('leaves tg: entries untouched (":\\d+@" needs an @, which tg: ids lack)', () => {
    const wl = parseWhitelist('tg:-1001234567890, tg:42');
    expect(wl.ids).toEqual(new Set(['tg:-1001234567890', 'tg:42']));
    expect(wl.phones.size).toBe(0);
  });
});

describe('phoneDigits strictness (the TS half of the TS/Python parity contract)', () => {
  it('rejects a doubled plus', () => {
    expect(parseWhitelist(`++${PHONE}`).phones.size).toBe(0);
  });

  it('rejects non-ASCII digits', () => {
    expect(parseWhitelist('\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668').phones.size).toBe(0);
  });

  it('rejects a trailing newline after the digits', () => {
    expect(parseWhitelist(`${PHONE}\n`).phones.has(PHONE)).toBe(true); // trim() eats it
    expect(parseWhitelist(`${PHONE}5\u000b`).phones.size).toBe(1); // vertical tab is punctuation
  });

  it('accepts a non-breaking space, as pasted from a web page', () => {
    expect(isWhitelisted(parseWhitelist('+49\u00a0157\u00a05594\u00a05319'), PHONE_JID)).toBe(true);
  });

  it('accepts the documented "." separator', () => {
    expect(isWhitelisted(parseWhitelist('49.157.5594.5319'), PHONE_JID)).toBe(true);
  });

  it('ignores numeric entries below the 5-digit floor', () => {
    expect(parseWhitelist('1234').phones.size).toBe(0);
  });
});
