import { describe, it, expect } from 'vitest';
import {
  phoneToJid,
  jidToPhone,
  isGroupChat,
  stripDeviceSuffix,
  normalizeJid,
  phoneFromJid,
} from '../../src/utils/jid.js';

// ---------------------------------------------------------------------------
// phoneToJid
// ---------------------------------------------------------------------------

describe('phoneToJid', () => {
  it('should convert a plain phone number to JID format', () => {
    expect(phoneToJid('16505551234')).toBe('16505551234@s.whatsapp.net');
  });

  it('should strip leading + before converting', () => {
    expect(phoneToJid('+16505551234')).toBe('16505551234@s.whatsapp.net');
  });

  it('should handle phone without country code prefix', () => {
    expect(phoneToJid('5511999999999')).toBe('5511999999999@s.whatsapp.net');
  });

  it('should handle phone with + prefix for Brazilian number', () => {
    expect(phoneToJid('+5511999999999')).toBe('5511999999999@s.whatsapp.net');
  });

  it('should not strip + in the middle of the string', () => {
    // Edge case: + not at the start should remain
    expect(phoneToJid('1650+555')).toBe('1650+555@s.whatsapp.net');
  });

  it('should handle short phone numbers', () => {
    expect(phoneToJid('12345')).toBe('12345@s.whatsapp.net');
  });
});

// ---------------------------------------------------------------------------
// jidToPhone
// ---------------------------------------------------------------------------

describe('jidToPhone', () => {
  it('should extract phone number from standard JID', () => {
    expect(jidToPhone('16505551234@s.whatsapp.net')).toBe('16505551234');
  });

  it('should extract identifier from group JID', () => {
    expect(jidToPhone('120363012345678@g.us')).toBe('120363012345678');
  });

  it('should return input as-is if no @ present', () => {
    expect(jidToPhone('16505551234')).toBe('16505551234');
  });

  it('should handle JID with device suffix', () => {
    expect(jidToPhone('5491126726818:50@s.whatsapp.net')).toBe('5491126726818:50');
  });

  it('should handle empty string', () => {
    expect(jidToPhone('')).toBe('');
  });

  it('should handle JID with LID domain', () => {
    expect(jidToPhone('123456789@lid')).toBe('123456789');
  });
});

// ---------------------------------------------------------------------------
// isGroupChat
// ---------------------------------------------------------------------------

describe('isGroupChat', () => {
  it('should return true for group JID', () => {
    expect(isGroupChat('120363012345678@g.us')).toBe(true);
  });

  it('should return false for individual JID', () => {
    expect(isGroupChat('16505551234@s.whatsapp.net')).toBe(false);
  });

  it('should return false for LID', () => {
    expect(isGroupChat('123456789@lid')).toBe(false);
  });

  it('should return false for plain phone number', () => {
    expect(isGroupChat('16505551234')).toBe(false);
  });

  it('should return false for empty string', () => {
    expect(isGroupChat('')).toBe(false);
  });

  it('should be case-sensitive (G.US is not a group)', () => {
    expect(isGroupChat('120363012345678@G.US')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// stripDeviceSuffix
// ---------------------------------------------------------------------------

describe('stripDeviceSuffix', () => {
  it('should remove device suffix from JID', () => {
    expect(stripDeviceSuffix('5491126726818:50@s.whatsapp.net')).toBe(
      '5491126726818@s.whatsapp.net'
    );
  });

  it('should return JID unchanged if no device suffix', () => {
    expect(stripDeviceSuffix('16505551234@s.whatsapp.net')).toBe('16505551234@s.whatsapp.net');
  });

  it('should handle device suffix with single digit', () => {
    expect(stripDeviceSuffix('16505551234:0@s.whatsapp.net')).toBe(
      '16505551234@s.whatsapp.net'
    );
  });

  it('should handle device suffix with multiple digits', () => {
    expect(stripDeviceSuffix('16505551234:123@s.whatsapp.net')).toBe(
      '16505551234@s.whatsapp.net'
    );
  });

  it('should not modify group JIDs (no device suffix pattern)', () => {
    expect(stripDeviceSuffix('120363012345678@g.us')).toBe('120363012345678@g.us');
  });

  it('should not modify plain phone numbers', () => {
    expect(stripDeviceSuffix('16505551234')).toBe('16505551234');
  });

  it('should handle empty string', () => {
    expect(stripDeviceSuffix('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// normalizeJid
// ---------------------------------------------------------------------------

describe('normalizeJid', () => {
  it('should return JID as-is if it already contains @', () => {
    expect(normalizeJid('16505551234@s.whatsapp.net')).toBe('16505551234@s.whatsapp.net');
  });

  it('should convert phone number to JID if no @', () => {
    expect(normalizeJid('16505551234')).toBe('16505551234@s.whatsapp.net');
  });

  it('should return group JID as-is', () => {
    expect(normalizeJid('120363012345678@g.us')).toBe('120363012345678@g.us');
  });

  it('should return LID as-is', () => {
    expect(normalizeJid('123456789@lid')).toBe('123456789@lid');
  });

  it('should convert phone with + prefix to JID', () => {
    expect(normalizeJid('+16505551234')).toBe('16505551234@s.whatsapp.net');
  });

  it('should preserve JIDs with device suffix', () => {
    expect(normalizeJid('5491126726818:50@s.whatsapp.net')).toBe(
      '5491126726818:50@s.whatsapp.net'
    );
  });
});

// ---------------------------------------------------------------------------
// phoneFromJid
// ---------------------------------------------------------------------------

describe('phoneFromJid', () => {
  it('should extract phone with + prefix from standard JID', () => {
    expect(phoneFromJid('16505551234@s.whatsapp.net')).toBe('+16505551234');
  });

  it('should return null for group JID', () => {
    expect(phoneFromJid('120363012345678@g.us')).toBeNull();
  });

  it('should return null for LID', () => {
    expect(phoneFromJid('123456789@lid')).toBeNull();
  });

  it('should return null for plain phone number without domain', () => {
    expect(phoneFromJid('16505551234')).toBeNull();
  });

  it('should handle Brazilian phone number JID', () => {
    expect(phoneFromJid('5511999999999@s.whatsapp.net')).toBe('+5511999999999');
  });

  it('should handle JID with device suffix (includes suffix in result)', () => {
    // The split('@')[0] includes the device suffix
    expect(phoneFromJid('5491126726818:50@s.whatsapp.net')).toBe('+5491126726818:50');
  });
});
