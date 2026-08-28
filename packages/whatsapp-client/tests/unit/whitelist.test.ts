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
